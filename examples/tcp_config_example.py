#!/usr/bin/env python3
"""Minimal example of changing gateway settings over TCP."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

from xgt_gateway.control_client import request  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=15150)
    args = parser.parse_args()
    result = request(
        args.host,
        args.port,
        "update_config",
        {"patch": {"read": {"address": "D0", "byte_count": 100, "interval_ms": 100}}},
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
