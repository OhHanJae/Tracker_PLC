#!/usr/bin/env python3
"""Run the XGT shared-memory gateway service."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from xgt_gateway.config import ConfigStore  # noqa: E402
from xgt_gateway.logging_setup import configure_logging  # noqa: E402
from xgt_gateway.service import GatewayService  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LS ELECTRIC XGT continuous-BYTE shared-memory gateway")
    parser.add_argument(
        "--config",
        default=str(PROJECT_DIR / "config.json"),
        help="JSON configuration path (default: project config.json)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = ConfigStore(args.config)
    try:
        config, created = store.load_or_create()
    except Exception as exc:
        print(f"[ERROR] Configuration load failed: {exc}", file=sys.stderr)
        return 1
    configure_logging(config, PROJECT_DIR)

    print("\nXGT Shared Memory Gateway 1.0.0")
    print(f"Config: {store.path}")
    if created:
        print("A new configuration file was created.")
        print("Control TCP and Web UI are available without an auth token.")

    service: GatewayService | None = None
    stop_event = threading.Event()

    def request_stop(_signum=None, _frame=None) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    try:
        service = GatewayService(store, config)
        service.start()
        status = service.status.snapshot()
        print(f"Control TCP: {status['control_endpoint']}")
        if status["web_running"]:
            print(f"Web UI: {status['web_url']}")
        print(f"Shared memory: {service.shared_memory().name}")
        print("Press Ctrl+C to stop.\n")
        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.getLogger(__name__).exception("Gateway startup/runtime failure")
        print(f"[ERROR] Gateway failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
