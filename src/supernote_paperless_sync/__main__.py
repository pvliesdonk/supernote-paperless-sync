"""Entry point for supernote-paperless-sync.

Runs two concurrent loops:
  - inbound: watches Supernote Note/ for .note files → Paperless
  - outbound: polls Paperless for 'send-to-supernote' tagged docs → Supernote
"""

from __future__ import annotations

import asyncio
import logging
import sys

from .config import Settings
from .db import init_db
from .inbound import run_inbound_watcher
from .outbound import run_outbound_sync
from .paperless import PaperlessClient


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        level=getattr(logging, level.upper(), logging.INFO),
    )


async def _main() -> None:
    settings = Settings()
    _configure_logging(settings.log_level)

    log = logging.getLogger(__name__)
    log.info(
        "starting inbound_tag=%s outbound_tag=%s poll_interval=%ds",
        settings.inbound_tag,
        settings.outbound_tag,
        settings.poll_interval,
    )

    init_db(settings.state_db)

    # Sync client: safe to construct here, used only inside asyncio.to_thread()
    client = PaperlessClient(settings.paperless_url, settings.paperless_token)

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(run_inbound_watcher(settings, client), name="inbound")
            tg.create_task(run_outbound_sync(settings, client), name="outbound")
    except* RuntimeError as eg:
        for exc in eg.exceptions:
            log.critical("startup_error error=%s", exc)
        sys.exit(1)
    finally:
        client.close()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
