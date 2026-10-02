#!/usr/bin/env python3
"""Launch the PySide6 remote configuration application."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR / "src"))
sys.path.insert(0, str(PROJECT_DIR / "ui"))

from xgt_config_client import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

