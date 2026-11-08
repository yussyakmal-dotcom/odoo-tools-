"""Single-worker queued, read-only Odoo BOM snapshot synchronization."""
import asyncio
import logging
import signal
import threading

from .config import Settings
from .db import initialize
from .odoo_connection import initialize_schema
from .sync import claim_next_run, execute_run, initialize_sync_schema, recover_interrupted_runs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger(__name__)


def main() -> None:
    settings = Settings.from_env()
    initialize(settings.db_path)
    initialize_schema(settings.db_path)
    initialize_sync_schema(settings.db_path)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    recover_interrupted_runs(settings.db_path)
    LOGGER.info("Read-only BOM sync worker running")

    while not stop.is_set():
        try:
            run_id = claim_next_run(settings.db_path)
            if run_id is None:
                stop.wait(2.0)
                continue
            LOGGER.info("Sync run %s started", run_id)
            asyncio.run(execute_run(settings.db_path, run_id))
            LOGGER.info("Sync run %s finished", run_id)
        except Exception:
            LOGGER.exception("Worker loop issue; retrying safely")
            stop.wait(3.0)
    LOGGER.info("Sync worker stopped")


if __name__ == "__main__":
    main()
