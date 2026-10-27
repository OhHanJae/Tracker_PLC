import json
import socket
import time
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from xgt_gateway.config import default_config
from xgt_gateway.control_server import ControlServer
from xgt_gateway.plc_worker import PlcWorker
from xgt_gateway.shared_memory_bridge import SharedMemoryBridge, SharedMemoryClient, WRITE_GATE_OFF, WRITE_GATE_ON
from xgt_gateway.simulator import XgtPlcSimulator
from xgt_gateway.status import RuntimeStatus
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

    def test_external_client_cannot_overwrite_core_or_release_writer_before_ack(self):
        name = 'client-tx-' + uuid.uuid4().hex
        bridge = SharedMemoryBridge({'name': name, 'size': 512, 'read_offset': 64, 'write_offset': 264,
                                     'unlink_on_exit': True}, 200, 200)
        client = SharedMemoryClient(name)
        try:
            bridge.publish_read(b'R' * 200)
            with TxWriterLock(name):
                self.assertEqual(client.read_plc_data(), b'R' * 200)
                with self.assertRaises(OSError):
                    client.write_plc_data(b'X' * 200)
                self.assertEqual(bridge.read_write_area(), (b'\0' * 200, 0))
            sequence = client.write_plc_data(b'A' * 200)
            self.assertEqual(bridge.read_write_area(), (b'A' * 200, sequence))
            self.assertFalse(client.write_acknowledged())
            with self.assertRaises(OSError):
                bridge.commit_write(b'B' * 200)
            bridge.acknowledge_write(sequence)
            self.assertTrue(client.write_acknowledged())
            client.close()
            bridge.commit_write(b'B' * 200)
        finally:
            client.close()
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


class ControllerWriteGateTests(unittest.TestCase):
    @staticmethod
    def wait_for(predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        raise AssertionError('Timed out waiting for the PLC simulator')

    @staticmethod
    def gate(bridge, enabled):
        info = bridge.info()
        offset = (max(info['read_offset'] + info['read_length'],
                      info['write_offset'] + info['write_length']) + 3) & ~3
        bridge._require_open().buf[offset:offset + 4] = WRITE_GATE_ON if enabled else WRITE_GATE_OFF

    @contextmanager
    def gateway(self, mode):
        simulator = XgtPlcSimulator()
        simulator.start()
        config = default_config()
        config['plc'].update(host=simulator.address[0], port=simulator.address[1],
                             connect_timeout_s=.5, io_timeout_s=.5)
        config['read'].update(address='D1000', byte_count=200, interval_ms=10)
        config['write'].update(address='D1100', byte_count=200, interval_ms=10,
                               mode=mode, write_on_startup=True)
        config['shared_memory'].update(name='gate-' + uuid.uuid4().hex, size=512,
                                       read_offset=64, write_offset=264)
        bridge = SharedMemoryBridge(config['shared_memory'], 200, 200)
        status = RuntimeStatus()
        worker = PlcWorker(lambda: config, lambda: bridge, status)
        writes = []
        original_write = simulator.write_memory
        def record(device, offset, data):
            original_write(device, offset, data)
            if device == 'D' and offset == 2200:
                writes.append(bytes(data))
        simulator.write_memory = record
        try:
            yield simulator, bridge, worker, status, writes
        finally:
            worker.stop()
            bridge.close(unlink=True)
            simulator.stop()

    def test_gate_requires_live_run_input_and_preserves_legacy_producer(self):
        with self.gateway('cyclic') as (_simulator, bridge, _worker, _status, _writes):
            self.assertTrue(bridge.controller_write_enabled(), 'absent marker keeps the legacy ABI')
            self.gate(bridge, False)
            self.assertFalse(bridge.controller_write_enabled())
            self.gate(bridge, True)
            self.assertFalse(bridge.controller_write_enabled(), 'CWG1 alone cannot authorize a write')
            bridge.publish_read(b'\x01\x00' + bytes(198))
            bridge.set_status(connected=True, read_ok=True)
            self.assertTrue(bridge.controller_write_enabled())
            bridge.publish_read(bytes(200))
            self.assertFalse(bridge.controller_write_enabled())
            bridge.publish_read(b'\x01\x00' + bytes(198))
            bridge.set_status(read_ok=False)
            self.assertFalse(bridge.controller_write_enabled())
            bridge.set_status(read_ok=True, connected=False)
            self.assertFalse(bridge.controller_write_enabled())
            self.assertEqual(bridge.info()['header_size'], 64)
            self.assertEqual(bridge.info()['read_length'], 200)
            self.assertEqual(bridge.info()['write_length'], 200)

    def test_real_tcp_off_on_cycles_only_transmit_fresh_commits_and_keep_reading(self):
        for mode in ('cyclic', 'on_change'):
            with self.subTest(mode=mode), self.gateway(mode) as (simulator, bridge, worker, status, writes):
                self.gate(bridge, False)
                bridge.commit_write(b'A' * 200)
                worker.start()
                self.wait_for(lambda: status.snapshot()['read_count'] >= 3)
                self.assertEqual(simulator.write_count, 0)
                self.assertEqual(bridge.info()['write_ack_sequence'], 0)
                for index in range(2):
                    simulator.write_memory('D', 2000, b'\x01\x00')
                    self.wait_for(lambda: bridge.read_read_area()[0][0] == 1)
                    reads = status.snapshot()['read_count']
                    self.wait_for(lambda: status.snapshot()['read_count'] >= reads + 3)
                    previous_writes, previous_ack = simulator.write_count, bridge.info()['write_ack_sequence']
                    fresh = bytes([66 + index]) * 200
                    sequence = bridge.commit_write(fresh)
                    self.gate(bridge, True)
                    sequence = bridge.commit_write(fresh)
                    self.wait_for(lambda: bridge.info()['write_ack_sequence'] == sequence)
                    self.assertEqual(simulator.read_memory('D', 2200, 200), fresh)
                    self.gate(bridge, False)
                    simulator.write_memory('D', 2000, b'\x00\x00')
                    self.wait_for(lambda: bridge.read_read_area()[0][0] == 0)
                    reads = status.snapshot()['read_count']
                    writes_after_stop = simulator.write_count
                    ack_after_stop = bridge.info()['write_ack_sequence']
                    bridge.commit_write(b'X' * 200)
                    self.wait_for(lambda: status.snapshot()['read_count'] >= reads + 4)
                    self.assertEqual(simulator.write_count, writes_after_stop)
                    self.assertEqual(bridge.info()['write_ack_sequence'], ack_after_stop)
                    self.assertGreater(writes_after_stop, previous_writes)
                    self.assertNotEqual(ack_after_stop, previous_ack)
                self.assertTrue(all(payload in {b'B' * 200, b'C' * 200} for payload in writes))

    def test_legacy_layout_without_spare_gate_bytes_remains_valid(self):
        bridge = SharedMemoryBridge({'name': 'legacy-gate-' + uuid.uuid4().hex, 'size': 464,
                                     'read_offset': 64, 'write_offset': 264, 'unlink_on_exit': True}, 200, 200)
        try:
            # Windows mappings may expose a full page beyond the declared ABI.
            memory = bridge._require_open().buf
            if len(memory) >= 468:
                memory[464:468] = WRITE_GATE_OFF
            self.assertTrue(bridge.controller_write_enabled(), 'bytes outside declared size cannot enable a Core extension')
        finally:
            bridge.close(unlink=True)

    def test_resume_does_not_replay_a_cyclic_payload_when_core_missed_run_off(self):
        for mode in ('cyclic', 'on_change'):
            with self.subTest(mode=mode), self.gateway(mode) as (simulator, bridge, worker, status, writes):
                simulator.write_memory('D', 2000, b'\x01\x00')
                self.gate(bridge, True)
                sequence = bridge.commit_write(b'A' * 200)
                worker.start()
                self.wait_for(lambda: bridge.info()['write_ack_sequence'] == sequence)
                simulator.write_memory('D', 2000, b'\x00\x00')
                self.wait_for(lambda: bridge.read_read_area()[0][0] == 0)
                reads = status.snapshot()['read_count']
                self.wait_for(lambda: status.snapshot()['read_count'] >= reads + 3)
                previous_writes = simulator.write_count
                simulator.write_memory('D', 2000, b'\x01\x00')
                reads = status.snapshot()['read_count']
                self.wait_for(lambda: status.snapshot()['read_count'] >= reads + 4)
                self.assertEqual(simulator.write_count, previous_writes, 'RUN ON needs a fresh Core commit')
                fresh_sequence = bridge.commit_write(b'B' * 200)
                self.wait_for(lambda: bridge.info()['write_ack_sequence'] == fresh_sequence)
                if mode == 'on_change':
                    self.assertEqual(writes[previous_writes:], [b'B' * 200])
                self.assertTrue(all(payload == b'B' * 200 for payload in writes[previous_writes:]))

    def test_fresh_gate_does_not_authorize_a_snapshot_copied_before_core_commit(self):
        with self.gateway('on_change') as (simulator, bridge, worker, _status, writes):
            simulator.write_memory('D', 2000, b'\x01\x00')
            self.gate(bridge, True)
            bridge.commit_write(b'A' * 200)
            original_read = bridge.read_write_area
            injected = False
            fresh_sequence = None
            def replace_after_snapshot(*args, **kwargs):
                nonlocal injected, fresh_sequence
                result = original_read(*args, **kwargs)
                if not injected and bridge.info()['status_flags'] & 3 == 3:
                    injected = True
                    self.gate(bridge, False)
                    fresh_sequence = bridge.commit_write(b'B' * 200)
                    self.gate(bridge, True)
                return result
            with patch.object(bridge, 'read_write_area', side_effect=replace_after_snapshot):
                worker.start()
                self.wait_for(lambda: fresh_sequence is not None and bridge.info()['write_ack_sequence'] == fresh_sequence)
            self.assertEqual(writes, [b'B' * 200], 'a post-snapshot gate transition must not send old TX bytes')

    def test_legacy_producer_still_writes_without_run_bit_or_extension(self):
        for mode in ('cyclic', 'on_change'):
            with self.subTest(mode=mode), self.gateway(mode) as (simulator, bridge, worker, status, _writes):
                sequence = bridge.commit_write(b'L' * 200)
                worker.start()
                self.wait_for(lambda: bridge.info()['write_ack_sequence'] == sequence)
                reads, writes = status.snapshot()['read_count'], simulator.write_count
                self.wait_for(lambda: status.snapshot()['read_count'] >= reads + 4)
                self.assertEqual(simulator.read_memory('D', 2200, 200), b'L' * 200)
                self.assertGreater(simulator.write_count, writes) if mode == 'cyclic' else self.assertEqual(simulator.write_count, writes)
