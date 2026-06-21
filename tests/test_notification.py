"""Tests de l'executor notification (BUILD_BRIEF.md commit 5).

Autonome : un faux invoker remplace le fleet MCP (aucun reseau).
    python -m tests.test_notification
Verifie la projection du payload, la construction des ToolCall par canal,
la validation des champs requis, la gestion des erreurs et le bout-en-bout.
"""

import asyncio

from scheduler_mcp.executors.base import Dispatcher
from scheduler_mcp.executors.channels import (
    NotificationMessage,
    ToolCall,
    default_channels,
)
from scheduler_mcp.executors.notification import (
    NotificationExecutor,
    UnconfiguredInvoker,
)


class FakeInvoker:
    """Enregistre les ToolCall et retourne un succes."""

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def call(self, call: ToolCall) -> dict:
        self.calls.append(call)
        return {"ok": True}


class RaisingInvoker:
    async def call(self, call: ToolCall) -> dict:
        raise RuntimeError("transport down")


def run(coro):
    return asyncio.run(coro)


def test_from_payload_alias() -> None:
    msg = NotificationMessage.from_payload(
        {"channel": "email", "to": "x@y.com", "objet": "Hi", "texte": "coucou"}
    )
    assert msg.canal == "email"
    assert msg.destinataire == "x@y.com"
    assert msg.sujet == "Hi"
    assert msg.corps == "coucou"


def test_email_tool_call() -> None:
    execu = NotificationExecutor(FakeInvoker())
    channels = default_channels()
    msg = NotificationMessage.from_payload(
        {"canal": "email", "destinataire": "a@b.c", "sujet": "Sujet", "message": "Corps"}
    )
    call = channels["email"].build_call(msg)
    assert call.server == "imap"
    assert call.tool == "imap_send_email"
    assert call.arguments == {"to": "a@b.c", "subject": "Sujet", "body": "Corps"}


def test_whatsapp_et_sms_tool_call() -> None:
    channels = default_channels()
    wa = channels["whatsapp"].build_call(
        NotificationMessage.from_payload(
            {"canal": "whatsapp", "destinataire": "+33...", "message": "salut"}
        )
    )
    assert wa.server == "whatsapp" and wa.tool == "send_message"
    assert wa.arguments == {"recipient": "+33...", "message": "salut"}

    sms = channels["twilio"].build_call(  # alias twilio -> sms
        NotificationMessage.from_payload(
            {"canal": "twilio", "destinataire": "+33...", "message": "salut"}
        )
    )
    assert sms.server == "twilio" and sms.tool == "send_sms"
    assert sms.arguments == {"to": "+33...", "body": "salut"}


def test_options_passthrough() -> None:
    channels = default_channels()
    call = channels["email"].build_call(
        NotificationMessage.from_payload(
            {"canal": "email", "destinataire": "a@b.c", "message": "x",
             "options": {"account_id": "acc-1", "cc": "z@y.c"}}
        )
    )
    assert call.arguments["account_id"] == "acc-1"
    assert call.arguments["cc"] == "z@y.c"


async def _execute(payload, invoker=None):
    execu = NotificationExecutor(invoker or FakeInvoker())
    return await execu.execute({"id": 1, "type": "notification", "payload": payload})


def test_execute_succes() -> None:
    invoker = FakeInvoker()
    execu = NotificationExecutor(invoker)
    res = run(execu.execute({"id": 1, "type": "notification",
                             "payload": {"canal": "email", "destinataire": "a@b.c",
                                         "message": "hello"}}))
    assert res.result == "success"
    assert len(invoker.calls) == 1
    assert invoker.calls[0].tool == "imap_send_email"


def test_execute_payload_json_string() -> None:
    invoker = FakeInvoker()
    execu = NotificationExecutor(invoker)
    res = run(execu.execute({"id": 1, "type": "notification",
                             "payload": '{"canal":"sms","destinataire":"+1","message":"yo"}'}))
    assert res.result == "success"
    assert invoker.calls[0].tool == "send_sms"


def test_execute_erreurs() -> None:
    # payload vide
    assert run(_execute(None)).result == "failure"
    # canal absent
    assert run(_execute({"destinataire": "a@b.c", "message": "x"})).result == "failure"
    # canal inconnu
    assert run(_execute({"canal": "pigeon", "destinataire": "a", "message": "x"})).result == "failure"
    # destinataire manquant
    assert run(_execute({"canal": "email", "message": "x"})).result == "failure"
    # corps manquant
    assert run(_execute({"canal": "email", "destinataire": "a@b.c"})).result == "failure"


def test_execute_invoker_en_erreur() -> None:
    res = run(_execute({"canal": "email", "destinataire": "a@b.c", "message": "x"},
                       invoker=RaisingInvoker()))
    assert res.result == "failure"
    assert "transport down" in res.detail


def test_unconfigured_invoker() -> None:
    res = run(_execute({"canal": "email", "destinataire": "a@b.c", "message": "x"},
                       invoker=UnconfiguredInvoker()))
    assert res.result == "failure"
    assert "MCP" in res.detail


def test_dispatch_via_dispatcher() -> None:
    invoker = FakeInvoker()
    disp = Dispatcher({"notification": NotificationExecutor(invoker)})
    res = run(disp.dispatch({"id": 1, "type": "notification",
                             "payload": {"canal": "whatsapp", "destinataire": "+1",
                                         "message": "hi"}}))
    assert res.result == "success"
    assert invoker.calls[0].server == "whatsapp"


def main() -> None:
    test_from_payload_alias()
    test_email_tool_call()
    test_whatsapp_et_sms_tool_call()
    test_options_passthrough()
    test_execute_succes()
    test_execute_payload_json_string()
    test_execute_erreurs()
    test_execute_invoker_en_erreur()
    test_unconfigured_invoker()
    test_dispatch_via_dispatcher()
    print("OK : tous les tests de notification passent")


if __name__ == "__main__":
    main()
