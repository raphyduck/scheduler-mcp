"""Executor agent (BUILD_BRIEF.md commit 7).

Appel de l'API Anthropic Messages avec le connecteur MCP : les serveurs du
toolset du job sont passes en mcp_servers, exposes au modele via des mcp_toolset,
et executes cote serveur Anthropic. La boucle relance la requete sur stop_reason
pause_turn (le serveur reprend la sequence d'outils) et trace l'integralite de la
conversation (texte, appels d'outils MCP, resultats) dans runs.detail.

Securite : Bitwarden est exclu du toolset, jamais auto-attribue a un job agent.
Le client Anthropic est injectable (tests) ; sinon construit a la demande depuis
la cle d'API. Aucun secret (cle, token) n'est logge.
"""

import json
from typing import Any, Optional

from ..config import Config
from ..logging_conf import get_logger
from .base import RunResult

log = get_logger("scheduler_mcp.executors.agent")

# Header beta du connecteur MCP de l'API Messages.
MCP_BETA = "mcp-client-2025-11-20"

# Serveurs jamais attribues automatiquement a un job agent (least privilege).
_BLOCKED_SERVERS = {"bitwarden", "bw"}

# Garde-fou contre une boucle pause_turn infinie.
_DEFAULT_MAX_ITERATIONS = 10
_TRACE_LIMIT = 2000


def _coerce_payload(payload) -> Optional[dict]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _get(block: Any, key: str, default=None):
    """Acces tolerant a un champ de bloc (objet SDK ou dict)."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _truncate(text: str) -> str:
    if len(text) > _TRACE_LIMIT:
        return text[:_TRACE_LIMIT] + f"... ({len(text) - _TRACE_LIMIT} caracteres tronques)"
    return text


def build_default_client(cfg: Config):
    """Construit un client Anthropic asynchrone. Import paresseux (dependance lourde)."""
    if not cfg.anthropic_api_key:
        return None
    import anthropic  # importe seulement si une cle est disponible

    return anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key)


class AgentExecutor:
    """Executor pour les jobs de type agent."""

    def __init__(
        self,
        cfg: Config,
        client: Any = None,
        server_registry: Optional[dict] = None,
        auth: Any = None,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._registry = server_registry if server_registry is not None else cfg.mcp_servers
        # Auth machine MCP : fournit le token a injecter dans les serveurs sans token propre.
        self._auth = auth

    def _resolve_servers(self, toolset: list[str], trace: list[str]) -> list[dict]:
        """Mappe le toolset du job vers des entrees mcp_servers, Bitwarden exclu."""
        servers: list[dict] = []
        for name in toolset or []:
            key = str(name).strip().lower()
            if key in _BLOCKED_SERVERS:
                trace.append(f"[securite] serveur {name!r} exclu (jamais auto-attribue)")
                log.warning("agent.serveur_bloque", serveur=name)
                continue
            entry = self._registry.get(key) or self._registry.get(name)
            if not entry or not entry.get("url"):
                trace.append(f"[avertissement] serveur MCP inconnu, ignore : {name!r}")
                continue
            server = {"type": "url", "name": key, "url": entry["url"]}
            if entry.get("authorization_token"):
                server["authorization_token"] = entry["authorization_token"]
            servers.append(server)
        return servers

    async def _inject_machine_token(self, servers: list[dict]) -> None:
        """Injecte le token machine dans les serveurs depourvus d'authorization_token."""
        if not servers or self._auth is None:
            return
        without = [s for s in servers if "authorization_token" not in s]
        if not without:
            return
        token = await self._auth.token()
        if not token:
            return
        for server in without:
            server["authorization_token"] = token

    async def execute(self, job: dict) -> RunResult:
        if self._client is None:
            return RunResult.fail("client Anthropic non configure (ANTHROPIC_API_KEY absent)")

        payload = _coerce_payload(job.get("payload"))
        if payload is None:
            return RunResult.fail("payload d'agent vide ou non JSON")

        instruction = (
            payload.get("instruction")
            or payload.get("prompt")
            or payload.get("message")
            or payload.get("tache")
        )
        if not instruction:
            return RunResult.fail("instruction de l'agent absente du payload")

        trace: list[str] = []
        servers = self._resolve_servers(job.get("toolset") or [], trace)
        await self._inject_machine_token(servers)

        kwargs: dict[str, Any] = {
            "model": payload.get("model") or self._cfg.llm_model,
            "max_tokens": int(payload.get("max_tokens") or self._cfg.llm_max_tokens),
            "messages": [{"role": "user", "content": str(instruction)}],
        }
        system = payload.get("system")
        if system:
            kwargs["system"] = str(system)
        if servers:
            kwargs["mcp_servers"] = servers
            kwargs["tools"] = [
                {"type": "mcp_toolset", "mcp_server_name": s["name"]} for s in servers
            ]
            kwargs["betas"] = [MCP_BETA]

        max_iterations = int(payload.get("max_iterations") or _DEFAULT_MAX_ITERATIONS)
        stop_reason = None
        try:
            for _ in range(max_iterations):
                response = await self._client.beta.messages.create(**kwargs)
                stop_reason = _get(response, "stop_reason")
                content = _get(response, "content", []) or []
                self._trace_response(content, stop_reason, _get(response, "usage"), trace)
                kwargs["messages"].append({"role": "assistant", "content": content})
                if stop_reason == "pause_turn":
                    continue  # le serveur reprend la sequence d'outils MCP
                break
        except Exception as exc:
            log.error("agent.exception", job=job.get("id"), error=str(exc), type=type(exc).__name__)
            trace.append(f"[erreur] exception {type(exc).__name__}: {exc}")
            return RunResult.fail("\n".join(trace))

        if stop_reason == "refusal":
            return RunResult.fail("\n".join(trace))
        if stop_reason == "pause_turn":
            trace.append(f"[avertissement] arret apres {max_iterations} iterations (pause_turn)")
            return RunResult.fail("\n".join(trace))
        log.info("agent.termine", job=job.get("id"), stop_reason=stop_reason)
        return RunResult.ok("\n".join(trace))

    def _trace_response(self, content, stop_reason, usage, trace: list[str]) -> None:
        for block in content:
            btype = _get(block, "type")
            if btype == "text":
                trace.append(_truncate(_get(block, "text", "") or ""))
            elif btype == "mcp_tool_use":
                server = _get(block, "server_name", "?")
                name = _get(block, "name", "?")
                args = json.dumps(_get(block, "input", {}), ensure_ascii=False, default=str)
                trace.append(f"[outil] {server}/{name} {_truncate(args)}")
            elif btype == "mcp_tool_result":
                tag = "erreur" if _get(block, "is_error", False) else "resultat"
                trace.append(f"[{tag}] {_truncate(self._result_text(_get(block, 'content', [])))}")
            elif btype:
                trace.append(f"[{btype}]")
        if usage is not None:
            trace.append(
                f"[usage] in={_get(usage, 'input_tokens', '?')} out={_get(usage, 'output_tokens', '?')}"
            )
        if stop_reason:
            trace.append(f"[stop] {stop_reason}")

    @staticmethod
    def _result_text(content) -> str:
        if isinstance(content, str):
            return content
        parts = [_get(item, "text", "") or "" for item in content or []]
        joined = " ".join(p for p in parts if p)
        return joined or json.dumps(content, ensure_ascii=False, default=str)
