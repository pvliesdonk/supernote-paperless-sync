"""Entry point for supernote-paperless-sync."""

from __future__ import annotations

import asyncio
import logging
import sys

import structlog

from .config import Settings
from .db import init_db
from .inbound import run_inbound_watcher
from .outbound import run_outbound_sync
from .paperless import PaperlessClient


def _configure_logging(level: str) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


async def _main() -> None:
    settings = Settings()  # type: ignore[call-arg]  # reads from env vars
    _configure_logging(settings.log_level)

    log = structlog.get_logger()
    log.info(
        "supernote_paperless_sync_starting",
        inbound_tag=settings.inbound_tag,
        outbound_tag=settings.outbound_tag,
        note_dir=str(settings.supernote_note_dir),
        doc_dir=str(settings.supernote_doc_dir),
        poll_interval=settings.poll_interval,
    )

    init_db(settings.state_db)
    log.info("state_db_ready", path=str(settings.state_db))

    client = PaperlessClient(settings.paperless_url, settings.paperless_token)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(run_inbound_watcher(settings, client), name="inbound")
            tg.create_task(run_outbound_sync(settings, client), name="outbound")
    except* RuntimeError as eg:
        log = structlog.get_logger()
        for exc in eg.exceptions:
            log.critical("startup_error", error=str(exc))
        sys.exit(1)
    finally:
        await client.aclose()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
