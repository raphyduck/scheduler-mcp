"""Tests du compiler / registration (BUILD_BRIEF.md commit 8).

Autonome : un faux client Anthropic renvoie la classification JSON (aucun reseau).
    python -m tests.test_compiler
Verifie la classification, le scope du toolset (least privilege + Bitwarden exclu),
le gate a_valider des scripts sensibles, et le repli heuristique sans LLM.
"""

import asyncio
import json
from types import SimpleNamespace

from scheduler_mcp.compiler import ALLOWED_TYPES, Compiler
from scheduler_mcp.config import Config


def make_cfg() -> Config:
    return Config(
        anthropic_api_key="sk-test", notion_token="", notion_version="2025-09-03",
        notion_programmation_ds="", notion_journal_db="", sqlite_path=":memory:",
        tick_interval_seconds=60, notion_sync_interval_seconds=300,
        max_concurrent_runs=4, lock_ttl_seconds=900, script_timeout_seconds=300,
        llm_model="claude-haiku-4-5", llm_max_tokens=4096, log_level="INFO",
        mcp_servers={},
    )


class FakeClient:
    """Faux client Anthropic : renvoie un bloc texte contenant le JSON fourni."""

    def __init__(self, text):
        self._text = text
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)],
            stop_reason="end_turn",
        )


def client_returning(obj) -> FakeClient:
    return FakeClient(json.dumps(obj) if not isinstance(obj, str) else obj)


def run(coro):
    return asyncio.run(coro)


def test_notification() -> None:
    raw = {"type": "notification", "schedule": "0 8 * * *",
           "payload": {"canal": "email", "destinataire": "x@y.z", "message": "coucou"},
           "toolset": ["imap"], "classif_reason": "envoi d'un message"}
    comp = Compiler(make_cfg(), client=client_returning(raw))
    job = run(comp.compile("rappelle-moi tous les matins"))
    assert job.type == "notification"
    assert job.payload["canal"] == "email"
    assert job.schedule == "0 8 * * *"
    assert job.toolset == []  # toolset force vide hors agent
    assert job.statut == "actif"


def test_script_benin() -> None:
    raw = {"type": "script", "payload": {"args": ["python3", "/data/backup.py"]},
           "toolset": [], "classif_reason": "execution d'un script"}
    comp = Compiler(make_cfg(), client=client_returning(raw))
    job = run(comp.compile("lance la sauvegarde"))
    assert job.type == "script"
    assert job.statut == "actif"
    assert job.sensitive_reasons == []


def test_script_sensible_a_valider() -> None:
    raw = {"type": "script", "payload": {"command": "rm -rf /data/old"},
           "classif_reason": "nettoyage"}
    comp = Compiler(make_cfg(), client=client_returning(raw))
    job = run(comp.compile("supprime les vieux fichiers"))
    assert job.type == "script"
    assert job.statut == "a_valider"
    assert job.sensitive_reasons  # non vide
    assert "sensible" in job.classif_reason


def test_agent_toolset_scope() -> None:
    raw = {"type": "agent",
           "payload": {"instruction": "trie mes mails"},
           "toolset": ["imap", "bitwarden", "ghost", "imap"],
           "classif_reason": "tache ouverte"}
    comp = Compiler(make_cfg(), client=client_returning(raw))
    job = run(comp.compile("trie ma boite mail"))
    assert job.type == "agent"
    # bitwarden exclu, ghost (inconnu) retire, dedupe.
    assert job.toolset == ["imap"]


def test_json_dans_du_texte() -> None:
    wrapped = 'Voici la tache :\n{"type":"agent","payload":{"instruction":"x"},"toolset":[]}\nVoila.'
    comp = Compiler(make_cfg(), client=client_returning(wrapped))
    job = run(comp.compile("fais quelque chose"))
    assert job.type == "agent"
    assert job.payload["instruction"] == "x"


def test_type_invalide_repli_heuristique() -> None:
    raw = {"type": "licorne", "payload": {}, "toolset": []}
    comp = Compiler(make_cfg(), client=client_returning(raw))
    job = run(comp.compile("envoie un email a Paul"))
    # Reponse invalide -> heuristique ; "email" -> notification.
    assert job.type == "notification"
    assert "heuristique" in job.classif_reason


def test_json_malforme_repli() -> None:
    comp = Compiler(make_cfg(), client=FakeClient("pas du json du tout"))
    job = run(comp.compile("execute le script de sauvegarde"))
    assert job.type == "script"  # mot-cle script/sauvegarde
    assert job.statut == "a_valider"  # repli degrade pour script


def test_sans_client_heuristique() -> None:
    comp = Compiler(make_cfg(), client=None)
    assert run(comp.compile("rappelle-moi d'appeler le dentiste")).type == "notification"
    assert run(comp.compile("classe les factures du trimestre")).type == "agent"


def test_entree_vide() -> None:
    comp = Compiler(make_cfg(), client=None)
    try:
        run(comp.compile("   "))
    except ValueError:
        pass
    else:
        raise AssertionError("ValueError attendue pour une entree vide")


def main() -> None:
    assert set(ALLOWED_TYPES) == {"notification", "script", "agent"}
    for test in [
        test_notification,
        test_script_benin,
        test_script_sensible_a_valider,
        test_agent_toolset_scope,
        test_json_dans_du_texte,
        test_type_invalide_repli_heuristique,
        test_json_malforme_repli,
        test_sans_client_heuristique,
        test_entree_vide,
    ]:
        test()
    print("OK : tous les tests du compiler passent")


if __name__ == "__main__":
    main()
