"""Point d'entree. Lance la boucle de tick et la boucle de sync Notion.

Skeleton runnable. La boucle de tick reste un stub tant que le commit 4
(voir BUILD_BRIEF.md) n'est pas implemente ; la sync Notion -> SQLite est active.
"""

import asyncio

from .config import Config, load_config
from .ledger import Ledger
from .logging_conf import get_logger, setup_logging
from .notion_sync import sync_once

log = get_logger("scheduler_mcp.main")


async def tick_loop(cfg: Config, ledger: Ledger) -> None:
    # TODO commit 4 : lire les jobs dus (next_run <= now, incl. retard) et dispatcher
    # vers un pool de workers borne (semaphore = max_concurrent_runs).
    while True:
        log.info("tick", note="stub, voir BUILD_BRIEF.md commit 4")
        await asyncio.sleep(cfg.tick_interval_seconds)


async def sync_loop(cfg: Config, ledger: Ledger) -> None:
    while True:
        try:
            await sync_once(cfg, ledger)
        except Exception as exc:  # la sync ne doit jamais tuer le service.
            log.error("notion_sync.error", error=str(exc), type=type(exc).__name__)
        await asyncio.sleep(cfg.notion_sync_interval_seconds)


async def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)
    log.info("scheduler_mcp.start", config=cfg.public_dict())
    ledger = await Ledger(cfg.sqlite_path).connect()
    log.info("ledger.ready", path=cfg.sqlite_path)
    try:
        await asyncio.gather(tick_loop(cfg, ledger), sync_loop(cfg, ledger))
    finally:
        await ledger.close()


if __name__ == "__main__":
    asyncio.run(main())
