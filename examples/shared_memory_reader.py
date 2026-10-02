#!/usr/bin/env python3
"""Read the latest stable PLC data from the gateway shared memory."""

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
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()
    with SharedMemoryClient(args.name) as memory:
        print(memory.layout())
        try:
            while True:
                print(memory.read_plc_data().hex(" ").upper())
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())

