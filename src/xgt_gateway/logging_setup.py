"""Console and rotating-file logging."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


def configure_logging(config: dict[str, Any], base_dir: Path) -> None:
    log_config = config["logging"]
    level = getattr(logging, log_config["level"])
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(threadName)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(level)
    root.addHandler(console)

    if log_config["file"]:
        path = Path(log_config["file"])
        if not path.is_absolute():
            path = base_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=log_config["max_bytes"],
            backupCount=log_config["backup_count"],
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root.addHandler(file_handler)

