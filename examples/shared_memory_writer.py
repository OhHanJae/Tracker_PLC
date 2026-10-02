#!/usr/bin/env python3
"""Commit exactly one TX payload for the gateway to write to the PLC."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

from xgt_gateway.shared_memory_bridge import SharedMemoryClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="xgt_gateway_v1")
    parser.add_argument(
        "--hex",
        required=True,
        help="Payload as hexadecimal bytes; length must match the configured write length",
    )
    parser.add_argument("--ack-timeout", type=float, default=3.0)
    args = parser.parse_args()
    payload = bytes.fromhex(args.hex)
    with SharedMemoryClient(args.name) as memory:
        sequence = memory.write_plc_data(payload)
        print(f"Committed sequence {sequence}; waiting for PLC ACK...")
        deadline = time.monotonic() + args.ack_timeout
        while time.monotonic() < deadline:
            if memory.write_acknowledged():
                print("PLC write acknowledged")
                return 0
            time.sleep(0.02)
    print("Timed out waiting for PLC write acknowledgement", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

