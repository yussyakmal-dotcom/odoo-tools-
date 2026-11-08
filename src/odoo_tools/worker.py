"""Separate worker process, reserved for upcoming read-only Odoo synchronization.

No BOM downloading or Odoo writes are implemented in this foundation milestone.
"""
import logging
import signal
import threading

from .config import Settings
from .db import initialize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    settings = Settings.from_env()
    initialize(settings.db_path)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    logging.info("Worker started; Odoo synchronization is not yet implemented")
    stop.wait()
    logging.info("Worker stopped")


if __name__ == "__main__":
    main()
