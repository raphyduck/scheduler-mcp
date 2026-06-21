"""Tests de l'executor agent (BUILD_BRIEF.md commit 7).

Autonome : un faux client Anthropic remplace l'API Messages (aucun reseau, pas
de dependance anthropic). Verifie le mapping du toolset vers mcp_servers, la
boucle pause_turn, la trace des appels d'outils, l'exclusion de Bitwarden et la
gestion des erreurs.
    python -m tests.test_agent
"""

import asyncio
from types import SimpleNamespace

from scheduler_mcp.config import Config
from scheduler_mcp.executors.agent import MCP_BETA, AgentExecutor


def make_cfg(mcp_servers=None) -> Config:
    return Config(
        anthropic_api_key="sk-test", notion_token="", notion_version="2025-09-03",
        notion_programmation_ds="", notion_journal_db="", sqlite_path=":memory:",
        tick_interval_seconds=60, notion_sync_interval_seconds=300,
        max_concurrent_runs=4, lock_ttl_seconds=900, script_timeout_seconds=300,
        llm_model="claude-haiku-4-5", llm_max_tokens=4096, log_level="INFO",
        mcp_servers=mcp_servers or {},
    )


class FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        messages = FakeMessages(responses)
        self.beta = SimpleNamespace(messages=messages)
        self.messages = messages  # raccourci pour les assertions


def text_block(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(server, name, inp):
    return SimpleNamespace(type="mcp_tool_use", server_name=server, name=name, input=inp)


def tool_result(text, is_error=False):
    return SimpleNamespace(
        type="mcp_tool_result", is_error=is_error,
        content=[SimpleNamespace(type="text", text=text)],
    )


def response(content, stop_reason="end_turn"):
    return SimpleNamespace(
        content=content, stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def run(coro):
    return asyncio.run(coro)


REGISTRY = {
    "imap": {"url": "https://fleet/imap/mcp", "authorization_token": "tok-imap"},
    "notion": {"url": "https://fleet/notion/mcp"},
}


def _job(payload, toolset=None):
    return {"id": 1, "type": "agent", "payload": payload, "toolset": toolset or []}


def test_texte_seul_sans_toolset() -> None:
    client = FakeClient([response([text_block("Bonjour")])])
    execu = AgentExecutor(make_cfg(), client=client)
    res = run(execu.execute(_job({"instruction": "dis bonjour"})))
    assert res.result == "success"
    assert "Bonjour" in res.detail
    assert len(client.messages.calls) == 1
    # Pas de toolset -> pas de connecteur MCP.
    assert "mcp_servers" not in client.messages.calls[0]


def test_mcp_pause_turn_et_trace() -> None:
    responses = [
        response(
            [tool_use("imap", "send_email", {"to": "x"}), tool_result("envoye")],
            stop_reason="pause_turn",
        ),
        response([text_block("Email envoye.")], stop_reason="end_turn"),
    ]
    client = FakeClient(responses)
    execu = AgentExecutor(make_cfg(REGISTRY), client=client)
    res = run(execu.execute(_job({"instruction": "envoie un email"}, toolset=["imap"])))

    assert res.result == "success"
    assert len(client.messages.calls) == 2  # pause_turn -> relance
    first = client.messages.calls[0]
    assert first["mcp_servers"] == [
        {"type": "url", "name": "imap", "url": "https://fleet/imap/mcp",
         "authorization_token": "tok-imap"}
    ]
    assert first["tools"] == [{"type": "mcp_toolset", "mcp_server_name": "imap"}]
    assert first["betas"] == [MCP_BETA]
    assert "[outil] imap/send_email" in res.detail
    assert "[resultat] envoye" in res.detail
    assert "Email envoye." in res.detail


def test_bitwarden_exclu() -> None:
    client = FakeClient([response([text_block("ok")])])
    execu = AgentExecutor(make_cfg(REGISTRY), client=client)
    res = run(execu.execute(_job({"instruction": "x"}, toolset=["bitwarden", "imap"])))
    servers = client.messages.calls[0]["mcp_servers"]
    assert [s["name"] for s in servers] == ["imap"]  # bitwarden retire
    assert "exclu" in res.detail


def test_serveur_inconnu_ignore() -> None:
    client = FakeClient([response([text_block("ok")])])
    execu = AgentExecutor(make_cfg(REGISTRY), client=client)
    res = run(execu.execute(_job({"instruction": "x"}, toolset=["ghost"])))
    # Aucun serveur resolu -> pas de connecteur MCP du tout.
    assert "mcp_servers" not in client.messages.calls[0]
    assert "inconnu" in res.detail


def test_refus() -> None:
    client = FakeClient([response([], stop_reason="refusal")])
    execu = AgentExecutor(make_cfg(), client=client)
    res = run(execu.execute(_job({"instruction": "x"})))
    assert res.result == "failure"


def test_pause_turn_boucle_bornee() -> None:
    # Toujours pause_turn : la boucle doit s'arreter au plafond.
    responses = [response([text_block("...")], stop_reason="pause_turn") for _ in range(5)]
    client = FakeClient(responses)
    execu = AgentExecutor(make_cfg(), client=client)
    res = run(execu.execute(_job({"instruction": "x", "max_iterations": 2})))
    assert res.result == "failure"
    assert len(client.messages.calls) == 2
    assert "arret apres 2 iterations" in res.detail


def test_client_absent() -> None:
    execu = AgentExecutor(make_cfg(), client=None)
    res = run(execu.execute(_job({"instruction": "x"})))
    assert res.result == "failure"
    assert "non configure" in res.detail


def test_instruction_absente() -> None:
    client = FakeClient([response([text_block("ok")])])
    execu = AgentExecutor(make_cfg(), client=client)
    res = run(execu.execute(_job({"system": "tu es utile"})))
    assert res.result == "failure"
    assert "instruction" in res.detail


def test_payload_json_string_et_system() -> None:
    client = FakeClient([response([text_block("ok")])])
    execu = AgentExecutor(make_cfg(), client=client)
    res = run(execu.execute(_job('{"instruction": "salut", "system": "sois bref"}')))
    assert res.result == "success"
    assert client.messages.calls[0]["system"] == "sois bref"
    assert client.messages.calls[0]["messages"][0]["content"] == "salut"


def main() -> None:
    for test in [
        test_texte_seul_sans_toolset,
        test_mcp_pause_turn_et_trace,
        test_bitwarden_exclu,
        test_serveur_inconnu_ignore,
        test_refus,
        test_pause_turn_boucle_bornee,
        test_client_absent,
        test_instruction_absente,
        test_payload_json_string_et_system,
    ]:
        test()
    print("OK : tous les tests de l'executor agent passent")


if __name__ == "__main__":
    main()
