"""Ecriture Journal Notion (BUILD_BRIEF.md commit 9).

Apres chaque run, append d'une entree dans la base Journal (log append-only).
Agent vaut toujours « Claude (assistant) ». Les noms de proprietes doivent
correspondre exactement a la base Notion, accents compris : Action (titre),
Détail (texte), Source (texte), Agent (texte), Type (select).

Le parent de page utilise data_source_id si NOTION_JOURNAL_DS est renseigne
(API 2025-09-03), sinon database_id (NOTION_JOURNAL_DB). Les erreurs Journal
n'echouent jamais un run : le run est deja cloture cote ledger.
"""

from typing import Any, Optional

from .config import Config
from .logging_conf import get_logger

log = get_logger("scheduler_mcp.journal")

# Noms de proprietes Notion, exacts (accents obligatoires).
FIELD_ACTION = "Action"
FIELD_DETAIL = "Détail"
FIELD_SOURCE = "Source"
FIELD_AGENT = "Agent"
FIELD_TYPE = "Type"

AGENT = "Claude (assistant)"
SOURCE = "scheduler-mcp"

# Notion limite un objet rich_text a 2000 caracteres.
_TEXT_LIMIT = 1900


def _rich(content: str) -> list[dict]:
    text = (content or "")[:_TEXT_LIMIT]
    return [{"type": "text", "text": {"content": text}}] if text else []


class Journal:
    """Ecrit les entrees du Journal Notion apres chaque run."""

    def __init__(self, cfg: Config, notion: Any = None) -> None:
        self._cfg = cfg
        self._notion = notion

    @property
    def enabled(self) -> bool:
        return bool(
            self._notion is not None
            and self._cfg.notion_token
            and self._cfg.notion_journal_db
        )

    def _parent(self) -> dict:
        if self._cfg.notion_journal_ds:
            return {"type": "data_source_id", "data_source_id": self._cfg.notion_journal_ds}
        return {"type": "database_id", "database_id": self._cfg.notion_journal_db}

    def build_properties(self, nom: str, type_: str, result: str, detail: str) -> dict:
        """Construit les proprietes de la page Journal pour un run."""
        action = f"{nom} : {result}" if nom else result
        return {
            FIELD_ACTION: {"title": _rich(action)},
            FIELD_DETAIL: {"rich_text": _rich(detail or "")},
            FIELD_SOURCE: {"rich_text": _rich(SOURCE)},
            FIELD_AGENT: {"rich_text": _rich(AGENT)},
            FIELD_TYPE: {"select": {"name": type_ or "agent"}},
        }

    async def record(
        self, *, nom: str, type_: str, result: str, detail: str
    ) -> Optional[str]:
        """Append une entree Journal ; retourne l'id de page, ou None si saute/erreur."""
        if not self.enabled:
            return None
        try:
            page = await self._notion.create_page(
                self._parent(), self.build_properties(nom, type_, result, detail)
            )
        except Exception as exc:
            log.error("journal.echec", error=str(exc), type=type(exc).__name__)
            return None
        page_id = page.get("id") if isinstance(page, dict) else None
        log.info("journal.append", page=page_id, result=result)
        return page_id
