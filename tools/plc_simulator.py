#!/usr/bin/env python3
"""Run the included XGT/TCP PLC simulator."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

from xgt_gateway.simulator import XgtPlcSimulator  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="XGT continuous-BYTE test PLC simulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2004)
    parser.add_argument("--size", type=int, default=65536)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    simulator = XgtPlcSimulator(args.host, args.port, args.size)
    simulator.fill_incrementing("D")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    simulator.start()
    print(f"XGT PLC simulator: {simulator.address[0]}:{simulator.address[1]}")
    print("D byte area is initialized with 00 01 02 ... FF pattern.")
    print("Press Ctrl+C to stop.")
    try:
        while not stop.wait(0.5):
            pass
    finally:
        simulator.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

