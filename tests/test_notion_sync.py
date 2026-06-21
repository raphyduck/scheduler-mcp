"""Tests de la sync Notion -> SQLite (BUILD_BRIEF.md commit 3).

Autonome : un faux client Notion remplace l'API REST (aucun reseau requis).
    python -m tests.test_notion_sync
Verifie l'extraction tolerante aux accents, le calcul de next_run (cron et
one-shot), le write-back limite aux changements et le lifecycle one-shot.
"""

import asyncio
import tempfile
from pathlib import Path

from scheduler_mcp.config import Config
from scheduler_mcp.ledger import Ledger, parse_iso
from scheduler_mcp.notion_sync import (
    build_write_back,
    compute_next_run,
    is_one_shot,
    parse_programmation_page,
    sync_once,
)


class FakeNotion:
    """Faux NotionClient : sert des pages canned et enregistre les write-backs."""

    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages
        self.updates: list[tuple[str, dict]] = []

    async def query_data_source(self, data_source_id: str) -> list[dict]:
        return self._pages

    async def update_page(self, page_id: str, properties: dict) -> dict:
        self.updates.append((page_id, properties))
        return {}


def make_cfg() -> Config:
    return Config(
        anthropic_api_key="",
        notion_token="fake-token",
        notion_version="2025-09-03",
        notion_programmation_ds="ds-1",
        notion_journal_db="",
        sqlite_path=":memory:",
        tick_interval_seconds=60,
        notion_sync_interval_seconds=300,
        max_concurrent_runs=4,
        lock_ttl_seconds=900,
        script_timeout_seconds=300,
        llm_model="claude-haiku-4-5",
        llm_max_tokens=4096,
        log_level="INFO",
        mcp_servers={},
    )


def make_page(
    page_id: str,
    nom: str,
    *,
    type_="notification",
    schedule=None,
    statut="actif",
    toolset=None,
    last_run=None,
    next_run=None,
    accents=True,
) -> dict:
    """Construit une page Notion Programmation. accents=True utilise les libelles
    accentues pour eprouver la resolution tolerante des noms de proprietes."""
    sched_key = "echeance/cron"
    last_key = "derniere execution"
    if accents:
        sched_key = "échéance/cron"  # echeance/cron accentue
        last_key = "dernière exécution"  # derniere execution accentue
    return {
        "id": page_id,
        "properties": {
            "Nom": {"type": "title", "title": [{"plain_text": nom}]},
            "type": {"type": "select", "select": {"name": type_} if type_ else None},
            sched_key: {
                "type": "rich_text",
                "rich_text": [{"plain_text": schedule}] if schedule else [],
            },
            "payload": {"type": "rich_text", "rich_text": []},
            "toolset": {
                "type": "multi_select",
                "multi_select": [{"name": t} for t in (toolset or [])],
            },
            "statut": {"type": "select", "select": {"name": statut} if statut else None},
            "prochain run": {"type": "date", "date": {"start": next_run} if next_run else None},
            last_key: {"type": "date", "date": {"start": last_run} if last_run else None},
            "raison de classif": {"type": "rich_text", "rich_text": []},
        },
    }


def test_parse_tolerant_accents() -> None:
    page = make_page(
        "p1", "Rappel", schedule="*/5 * * * *", toolset=["imap", "notion"], accents=True
    )
    entry = parse_programmation_page(page)
    assert entry is not None
    assert entry.nom == "Rappel"
    assert entry.schedule == "*/5 * * * *"
    assert entry.toolset == ["imap", "notion"]
    assert entry.statut == "actif"
    # La cle reelle accentuee est capturee pour le write-back.
    assert entry.keys["schedule"] == "échéance/cron"
    assert entry.keys["last_run"] == "dernière exécution"


def test_parse_sans_titre() -> None:
    page = make_page("p0", "", schedule="*/5 * * * *")
    assert parse_programmation_page(page) is None


def test_is_one_shot() -> None:
    assert is_one_shot("2026-06-21T09:00:00") is True
    assert is_one_shot("*/5 * * * *") is False
    assert is_one_shot(None) is False
    assert is_one_shot("nonsense") is False


def test_compute_next_run_cron() -> None:
    now = "2026-06-20T12:02:00.000000Z"
    nxt = compute_next_run("*/5 * * * *", None, now)
    # Prochain creneau 5 min apres now, dans le futur.
    assert nxt is not None
    assert parse_iso(nxt) > parse_iso(now)
    # Ancre sur last_run : creneau strictement apres last_run.
    nxt2 = compute_next_run("*/5 * * * *", "2026-06-20T12:02:00.000000Z", now)
    assert parse_iso(nxt2) == parse_iso("2026-06-20T12:05:00Z")


def test_compute_next_run_one_shot() -> None:
    target = "2026-06-21T09:00:00"
    assert parse_iso(compute_next_run(target, None, "2026-06-20T12:00:00Z")) == parse_iso(target)
    # Deja execute : plus de prochain run.
    assert compute_next_run(target, "2026-06-21T09:00:05Z", "2026-06-22T00:00:00Z") is None


def test_compute_next_run_invalide() -> None:
    assert compute_next_run(None, None, "2026-06-20T12:00:00Z") is None
    assert compute_next_run("pas un cron", None, "2026-06-20T12:00:00Z") is None


def test_build_write_back_seulement_changements() -> None:
    page = make_page("p1", "Rappel", schedule="*/5 * * * *", statut="actif")
    entry = parse_programmation_page(page)
    # prochain run nouveau, pas de last_run, statut inchange -> un seul champ.
    out = build_write_back(page, entry, "2026-06-20T12:05:00.000000Z", None, "actif")
    assert list(out) == ["prochain run"]
    assert out["prochain run"]["date"]["start"] == "2026-06-20T12:05:00.000000Z"
    # Rien ne change -> dict vide (next_run identique a la seconde pres).
    page2 = make_page("p1", "Rappel", schedule="*/5 * * * *", next_run="2026-06-20T12:05:00Z")
    entry2 = parse_programmation_page(page2)
    out2 = build_write_back(page2, entry2, "2026-06-20T12:05:00.000000Z", None, "actif")
    assert out2 == {}


async def test_sync_once_cron(ledger: Ledger) -> None:
    cfg = make_cfg()
    page = make_page("page-cron", "Backup", schedule="*/5 * * * *", statut="actif",
                     toolset=["ssh"])
    notion = FakeNotion([page])
    count = await sync_once(cfg, ledger, notion=notion)
    assert count == 1

    job = await ledger.get_job_by_notion("page-cron")
    assert job is not None
    assert job["statut"] == "actif"
    assert job["toolset"] == ["ssh"]
    assert job["next_run"] is not None and parse_iso(job["next_run"]) is not None
    # Write-back du prochain run (Notion n'en avait pas).
    assert len(notion.updates) == 1
    page_id, props = notion.updates[0]
    assert page_id == "page-cron"
    assert "prochain run" in props


async def test_sync_once_one_shot_termine(ledger: Ledger) -> None:
    cfg = make_cfg()
    # One-shot deja execute (derniere execution renseignee), encore actif cote Notion.
    page = make_page(
        "page-os", "Envoi unique", schedule="2026-06-01T09:00:00",
        statut="actif", last_run="2026-06-01T09:00:05Z",
    )
    notion = FakeNotion([page])
    await sync_once(cfg, ledger, notion=notion)

    job = await ledger.get_job_by_notion("page-os")
    assert job["statut"] == "termine"
    assert job["next_run"] is None
    # Write-back du statut termine.
    assert notion.updates, "un write-back de statut est attendu"
    _, props = notion.updates[0]
    assert props.get("statut", {}).get("select", {}).get("name") == "termine"


async def test_sync_once_skip_sans_token() -> None:
    cfg = Config(
        anthropic_api_key="", notion_token="", notion_version="2025-09-03",
        notion_programmation_ds="", notion_journal_db="", sqlite_path=":memory:",
        tick_interval_seconds=60, notion_sync_interval_seconds=300,
        max_concurrent_runs=4, lock_ttl_seconds=900, script_timeout_seconds=300,
        llm_model="x", llm_max_tokens=4096, log_level="INFO", mcp_servers={},
    )
    async with Ledger(":memory:") as ledger:
        assert await sync_once(cfg, ledger, notion=FakeNotion([])) == 0


async def main() -> None:
    # Tests synchrones (pure logique).
    test_parse_tolerant_accents()
    test_parse_sans_titre()
    test_is_one_shot()
    test_compute_next_run_cron()
    test_compute_next_run_one_shot()
    test_compute_next_run_invalide()
    test_build_write_back_seulement_changements()

    # Tests asynchrones avec ledger ephemere.
    with tempfile.TemporaryDirectory() as tmp:
        async def with_fresh(coro, name) -> None:
            path = str(Path(tmp) / f"{name}.db")
            async with Ledger(path) as ledger:
                await coro(ledger)

        await with_fresh(test_sync_once_cron, "cron")
        await with_fresh(test_sync_once_one_shot_termine, "os")
    await test_sync_once_skip_sans_token()

    print("OK : tous les tests de sync Notion passent")


if __name__ == "__main__":
    asyncio.run(main())
