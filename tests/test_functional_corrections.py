import json
import socket
import unittest
import uuid

from xgt_gateway.control_server import ControlServer
from xgt_gateway.shared_memory_bridge import SharedMemoryBridge
from xgt_gateway.tx_writer_lock import TxWriterLock


class FunctionalCorrectionsTests(unittest.TestCase):
    def test_tx_writer_lock_blocks_other_producer_and_releases(self):
        name = 'lock-test-' + uuid.uuid4().hex
        first, second = TxWriterLock(name), TxWriterLock(name)
        try:
            first.acquire()
            with self.assertRaises(OSError):
                second.acquire()
            first.close()
            second.acquire()
        finally:
            first.close()
            second.close()

    def test_gateway_diagnostic_write_cannot_compete_with_core_lock(self):
        name = 'tx-test-' + uuid.uuid4().hex
        bridge = SharedMemoryBridge({'name': name, 'size': 512, 'read_offset': 64, 'write_offset': 264,
                                     'unlink_on_exit': True}, 200, 200)
        try:
            with TxWriterLock(name):
                with self.assertRaises(OSError):
                    bridge.commit_write(b'\0' * 200)
            bridge.commit_write(b'\x01' * 200)
            self.assertEqual(bridge.read_write_area()[0], b'\x01' * 200)
        finally:
            bridge.close(unlink=True)

    def test_bad_json_and_idle_connection_are_isolated_and_stop_closes_clients(self):
        requests = []
        def dispatch(command, params):
            requests.append((command, params))
            return {'alive': True}
        server = ControlServer('127.0.0.1', 0, lambda: 1024, dispatch, io_timeout_s=.15)
        server.start()
        connection = socket.create_connection(server.address, timeout=1)
        connection.settimeout(1)
        reader = connection.makefile('rb')
        try:
            connection.sendall(b'{"command":"ping","params":{"x":NaN}}\n')
            self.assertFalse(json.loads(reader.readline())['ok'])
            self.assertEqual(requests, [])
            connection.sendall(b'{"command":"ping","params":{}}\n')
            self.assertTrue(json.loads(reader.readline())['ok'])
            self.assertEqual(reader.readline(), b'')
        finally:
            reader.close()
            connection.close()
            server.stop()
