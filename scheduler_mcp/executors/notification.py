"""Executor notification (BUILD_BRIEF.md commit 5).

Envoi d'un message sur un canal (email via imap-mcp, WhatsApp, SMS via twilio).
Zero LLM : le payload du job decrit directement le message et le canal.

Le canal traduit le message en ToolCall (cf. channels.py) ; l'execution reelle
de l'appel est deleguee a un ToolInvoker. La couche outils MCP concrete arrive
au commit 11 ; en attendant, UnconfiguredInvoker echoue proprement (run failure).
"""

import json
from typing import Optional, Protocol, runtime_checkable

from ..logging_conf import get_logger
from .base import RunResult
from .channels import (
    Channel,
    NotificationMessage,
    ToolCall,
    default_channels,
    normalize_channel,
)

log = get_logger("scheduler_mcp.executors.notification")


@runtime_checkable
class ToolInvoker(Protocol):
    """Execute un appel d'outil du fleet MCP et retourne la reponse brute."""

    async def call(self, call: ToolCall) -> dict: ...


class UnconfiguredInvoker:
    """Invoker par defaut tant que la couche outils MCP n'est pas branchee."""

    async def call(self, call: ToolCall) -> dict:
        raise RuntimeError(
            "couche outils MCP non branchee (voir BUILD_BRIEF.md commit 11)"
        )


def _coerce_payload(payload) -> Optional[dict]:
    """Normalise le payload du job en dict, ou None si inexploitable."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


class NotificationExecutor:
    """Executor pour les jobs de type notification."""

    def __init__(
        self, invoker: ToolInvoker, channels: Optional[dict[str, Channel]] = None
    ) -> None:
        self._invoker = invoker
        self._channels = channels or default_channels()

    async def execute(self, job: dict) -> RunResult:
        payload = _coerce_payload(job.get("payload"))
        if payload is None:
            return RunResult.fail("payload de notification vide ou non JSON")

        message = NotificationMessage.from_payload(payload)
        if not message.canal:
            return RunResult.fail("canal de notification absent du payload")

        channel = self._channels.get(normalize_channel(message.canal))
        if channel is None:
            return RunResult.fail(f"canal de notification inconnu: {message.canal!r}")

        try:
            call = channel.build_call(message)
        except ValueError as exc:
            return RunResult.fail(str(exc))

        try:
            await self._invoker.call(call)
        except Exception as exc:
            log.error(
                "notification.echec", canal=message.canal, outil=call.tool,
                error=str(exc), type=type(exc).__name__,
            )
            return RunResult.fail(f"echec envoi {message.canal}: {exc}")

        detail = f"notification {message.canal} -> {message.destinataire} via {call.tool}"
        log.info("notification.envoyee", canal=message.canal, outil=call.tool)
        return RunResult.ok(detail)
