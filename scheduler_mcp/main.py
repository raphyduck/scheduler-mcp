"""Point d'entree. Lance la boucle de tick et la boucle de sync Notion.

La sync Notion -> SQLite et la boucle de tick (dispatch des jobs dus vers un pool
de workers borne) sont actives. Les executors concrets arrivent aux commits 5 a 7 ;
en attendant, un type sans executor produit un run skipped explicite.
"""

import asyncio

from .config import Config, load_config
from .executors.base import Dispatcher
from .executors.notification import NotificationExecutor, UnconfiguredInvoker
from .executors.script import ScriptExecutor
from .ledger import Ledger
from .logging_conf import get_logger, setup_logging
from .notion_sync import sync_once
from .tick import tick_loop

log = get_logger("scheduler_mcp.main")


async def sync_loop(cfg: Config, ledger: Ledger) -> None:
    while True:
        try:
            await sync_once(cfg, ledger)
        except Exception as exc:  # la sync ne doit jamais tuer le service.
            log.error("notion_sync.error", error=str(exc), type=type(exc).__name__)
        await asyncio.sleep(cfg.notion_sync_interval_seconds)


def build_dispatcher(cfg: Config) -> Dispatcher:
    # UnconfiguredInvoker tant que la couche outils MCP n'est pas branchee (commit 11).
    # Les executors script et agent s'enregistrent aux commits 6 et 7.
    dispatcher = Dispatcher()
    dispatcher.register("notification", NotificationExecutor(UnconfiguredInvoker()))
    dispatcher.register("script", ScriptExecutor(default_timeout=cfg.script_timeout_seconds))
    return dispatcher


async def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)
    log.info("scheduler_mcp.start", config=cfg.public_dict())
    ledger = await Ledger(cfg.sqlite_path).connect()
    log.info("ledger.ready", path=cfg.sqlite_path)
    dispatcher = build_dispatcher(cfg)
    try:
        await asyncio.gather(
            tick_loop(cfg, ledger, dispatcher), sync_loop(cfg, ledger)
        )
    finally:
        await ledger.close()


if __name__ == "__main__":
    asyncio.run(main())
