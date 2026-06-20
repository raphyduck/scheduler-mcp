"""Point d'entree. Lance la boucle de tick et la boucle de sync Notion.

Skeleton runnable. Les boucles sont des stubs tant que les commits 3 et 4
(voir BUILD_BRIEF.md) ne sont pas implementes.
"""

import asyncio

from .config import Config, load_config
from .logging_conf import get_logger, setup_logging

log = get_logger("scheduler_mcp.main")


async def tick_loop(cfg: Config) -> None:
    # TODO commit 4 : lire les jobs dus (next_run <= now, incl. retard) et dispatcher
    # vers un pool de workers borne (semaphore = max_concurrent_runs).
    while True:
        log.info("tick", note="stub, voir BUILD_BRIEF.md commit 4")
        await asyncio.sleep(cfg.tick_interval_seconds)


async def sync_loop(cfg: Config) -> None:
    # TODO commit 3 : sync base Notion Programmation -> ledger SQLite, calcul next_run,
    # write-back statut / derniere execution.
    while True:
        log.info("notion_sync", note="stub, voir BUILD_BRIEF.md commit 3")
        await asyncio.sleep(cfg.notion_sync_interval_seconds)


async def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)
    log.info("scheduler_mcp.start", config=cfg.public_dict())
    await asyncio.gather(tick_loop(cfg), sync_loop(cfg))


if __name__ == "__main__":
    asyncio.run(main())
