"""Tests de l'ecriture Journal Notion (BUILD_BRIEF.md commit 9).

Autonome : un faux client Notion remplace l'API (aucun reseau).
    python -m tests.test_journal
Verifie les noms de proprietes exacts (accents), l'Agent constant, le parent
(database_id vs data_source_id), le gating enabled et la resilience aux erreurs.
"""

import asyncio

from scheduler_mcp.config import Config
from scheduler_mcp.journal import (
    AGENT,
    FIELD_ACTION,
    FIELD_AGENT,
    FIELD_DETAIL,
    FIELD_SOURCE,
    FIELD_TYPE,
    Journal,
)


def make_cfg(token="tok", journal_db="db-1", journal_ds="") -> Config:
    return Config(
        anthropic_api_key="", notion_token=token, notion_version="2025-09-03",
        notion_programmation_ds="", notion_journal_db=journal_db, sqlite_path=":memory:",
        tick_interval_seconds=60, notion_sync_interval_seconds=300,
        max_concurrent_runs=4, lock_ttl_seconds=900, script_timeout_seconds=300,
        llm_model="x", llm_max_tokens=4096, log_level="INFO",
        notion_journal_ds=journal_ds, mcp_servers={},
    )


class FakeNotion:
    def __init__(self, raise_exc=False):
        self.calls = []
        self._raise = raise_exc

    async def create_page(self, parent, properties):
        self.calls.append((parent, properties))
        if self._raise:
            raise RuntimeError("notion down")
        return {"id": "page-123"}


def run(coro):
    return asyncio.run(coro)


def test_build_properties_champs_exacts() -> None:
    journal = Journal(make_cfg(), notion=FakeNotion())
    props = journal.build_properties("Backup", "script", "success", "code retour: 0")
    # Noms exacts, accent sur Detail.
    assert set(props) == {FIELD_ACTION, FIELD_DETAIL, FIELD_SOURCE, FIELD_AGENT, FIELD_TYPE}
    assert FIELD_DETAIL == "Détail"
    assert props[FIELD_ACTION]["title"][0]["text"]["content"] == "Backup : success"
    assert props[FIELD_AGENT]["rich_text"][0]["text"]["content"] == AGENT == "Claude (assistant)"
    assert props[FIELD_TYPE]["select"]["name"] == "script"
    assert props[FIELD_SOURCE]["rich_text"][0]["text"]["content"] == "scheduler-mcp"
    assert "code retour" in props[FIELD_DETAIL]["rich_text"][0]["text"]["content"]


def test_detail_vide() -> None:
    journal = Journal(make_cfg(), notion=FakeNotion())
    props = journal.build_properties("X", "agent", "failure", "")
    # rich_text vide accepte (liste vide).
    assert props[FIELD_DETAIL]["rich_text"] == []


def test_troncature() -> None:
    journal = Journal(make_cfg(), notion=FakeNotion())
    props = journal.build_properties("X", "agent", "success", "a" * 5000)
    assert len(props[FIELD_DETAIL]["rich_text"][0]["text"]["content"]) <= 1900


def test_parent_database_id() -> None:
    journal = Journal(make_cfg(journal_ds=""), notion=FakeNotion())
    parent = journal._parent()
    assert parent == {"type": "database_id", "database_id": "db-1"}


def test_parent_data_source_id() -> None:
    journal = Journal(make_cfg(journal_ds="ds-9"), notion=FakeNotion())
    parent = journal._parent()
    assert parent == {"type": "data_source_id", "data_source_id": "ds-9"}


def test_record_succes() -> None:
    notion = FakeNotion()
    journal = Journal(make_cfg(), notion=notion)
    page_id = run(journal.record(nom="Backup", type_="script", result="success", detail="ok"))
    assert page_id == "page-123"
    assert len(notion.calls) == 1
    parent, props = notion.calls[0]
    assert parent["database_id"] == "db-1"
    assert props[FIELD_AGENT]["rich_text"][0]["text"]["content"] == AGENT


def test_record_desactive_sans_notion() -> None:
    journal = Journal(make_cfg(), notion=None)
    assert journal.enabled is False
    assert run(journal.record(nom="X", type_="agent", result="success", detail="")) is None


def test_record_desactive_sans_db() -> None:
    journal = Journal(make_cfg(journal_db=""), notion=FakeNotion())
    assert journal.enabled is False


def test_record_erreur_resiliente() -> None:
    journal = Journal(make_cfg(), notion=FakeNotion(raise_exc=True))
    # Une erreur Notion ne crashe pas : retourne None.
    assert run(journal.record(nom="X", type_="agent", result="failure", detail="")) is None


def main() -> None:
    for test in [
        test_build_properties_champs_exacts,
        test_detail_vide,
        test_troncature,
        test_parent_database_id,
        test_parent_data_source_id,
        test_record_succes,
        test_record_desactive_sans_notion,
        test_record_desactive_sans_db,
        test_record_erreur_resiliente,
    ]:
        test()
    print("OK : tous les tests du Journal passent")


if __name__ == "__main__":
    main()
