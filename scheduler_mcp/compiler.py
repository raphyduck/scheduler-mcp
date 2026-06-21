"""Compiler / registration (BUILD_BRIEF.md commit 8).

Depuis une entree en langage naturel, produit un job structure : classer le type
(notification | script | agent), compiler le payload, scoper le toolset au strict
necessaire (least privilege), inferer une echeance, et ecrire la raison de classif.

Un appel LLM (Anthropic) fait la classification ; un repli heuristique (mots-cles)
prend le relais sans cle d'API ou si la reponse est inexploitable. Le type reste
modifiable a posteriori. Un script touchant du sensible est cree en statut
a_valider (gate via classify_sensitivity, commit 6). Bitwarden n'est jamais
attribue automatiquement.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Config
from .executors.script import classify_sensitivity
from .logging_conf import get_logger

log = get_logger("scheduler_mcp.compiler")

ALLOWED_TYPES = ("notification", "script", "agent")
# Outils MCP du fleet attribuables (Bitwarden volontairement absent).
ALLOWED_TOOLSETS = ("imap", "browser", "voicecall", "twilio", "whatsapp", "notion", "ssh")
_BLOCKED_TOOLSETS = {"bitwarden", "bw"}

_SYSTEM_PROMPT = (
    "Tu es un compilateur de taches programmees. A partir d'une demande en langage "
    "naturel, produis UNIQUEMENT un objet JSON (sans texte autour) avec les champs :\n"
    "- type : 'notification', 'script' ou 'agent'.\n"
    "  notification = envoyer un message sur un canal (email, whatsapp, sms).\n"
    "  script = executer une commande/script deterministe, sans LLM.\n"
    "  agent = tache ouverte necessitant un LLM et des outils.\n"
    "- schedule : expression cron a 5 champs, ou datetime ISO 8601 pour un one-shot, "
    "ou null si non precise.\n"
    "- payload : objet selon le type. notification : {canal, destinataire, sujet, message}. "
    "script : {command} ou {args:[...]}, options shell/env/cwd/timeout. agent : {instruction, system?}.\n"
    "- toolset : liste d'outils MCP STRICTEMENT necessaires, uniquement pour le type agent, "
    "parmi imap, browser, voicecall, twilio, whatsapp, notion, ssh. Sinon liste vide. "
    "N'inclus jamais bitwarden.\n"
    "- classif_reason : une phrase expliquant le choix du type.\n"
    "Reponds avec le seul JSON."
)


@dataclass
class CompiledJob:
    """Resultat de la compilation d'une entree en langage naturel."""

    type: str
    payload: dict
    toolset: list[str]
    classif_reason: str
    statut: str
    schedule: Optional[str] = None
    sensitive_reasons: list[str] = field(default_factory=list)


def _first_text(response: Any) -> str:
    content = response.get("content") if isinstance(response, dict) else getattr(response, "content", None)
    for block in content or []:
        btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if btype == "text":
            return (block.get("text") if isinstance(block, dict) else getattr(block, "text", "")) or ""
    return ""


def _extract_json(text: str) -> Optional[dict]:
    """Parse un objet JSON, tolerant a du texte autour."""
    text = (text or "").strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _scope_toolset(raw: Any, type_: str) -> list[str]:
    """Restreint le toolset aux outils autorises ; vide hors type agent ; Bitwarden exclu."""
    if type_ != "agent" or not isinstance(raw, list):
        return []
    scoped: list[str] = []
    for item in raw:
        name = str(item).strip().lower()
        if name in _BLOCKED_TOOLSETS:
            log.warning("compiler.bitwarden_refuse", outil=item)
            continue
        if name in ALLOWED_TOOLSETS and name not in scoped:
            scoped.append(name)
    return scoped


def _script_command_text(payload: dict) -> str:
    if payload.get("command"):
        return str(payload["command"])
    args = payload.get("args")
    if isinstance(args, list):
        return " ".join(str(a) for a in args)
    return ""


class Compiler:
    """Compile une entree en langage naturel en job structure."""

    def __init__(self, cfg: Config, client: Any = None) -> None:
        self._cfg = cfg
        self._client = client

    async def compile(self, text: str, nom: Optional[str] = None) -> CompiledJob:
        text = (text or "").strip()
        if not text:
            raise ValueError("entree vide a compiler")

        raw = await self._classify_llm(text) if self._client is not None else None
        if raw is None:
            return self._finalize(self._heuristic(text), heuristic=True)
        return self._finalize(raw, heuristic=False)

    async def _classify_llm(self, text: str) -> Optional[dict]:
        try:
            response = await self._client.messages.create(
                model=self._cfg.llm_model,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
            )
        except Exception as exc:
            log.error("compiler.llm_erreur", error=str(exc), type=type(exc).__name__)
            return None
        data = _extract_json(_first_text(response))
        if data is None or str(data.get("type", "")).lower() not in ALLOWED_TYPES:
            log.warning("compiler.reponse_invalide")
            return None
        return data

    def _heuristic(self, text: str) -> dict:
        """Classification de repli par mots-cles (LLM indisponible/invalide)."""
        low = text.lower()
        script_kw = ("script", "commande", "bash", "python", "cron", "sauvegarde",
                     "backup", "rsync", "./")
        notif_kw = ("rappel", "rappelle", "notifie", "previens", "envoie", "email",
                    "mail", "message", "sms", "whatsapp")
        if any(k in low for k in script_kw):
            type_ = "script"
            payload = {"command": text}
        elif any(k in low for k in notif_kw):
            type_ = "notification"
            payload = {"message": text}
        else:
            type_ = "agent"
            payload = {"instruction": text}
        return {
            "type": type_,
            "schedule": None,
            "payload": payload,
            "toolset": [],
            "classif_reason": f"classification heuristique ({type_}), LLM indisponible",
        }

    def _finalize(self, raw: dict, heuristic: bool) -> CompiledJob:
        type_ = str(raw.get("type", "agent")).lower()
        if type_ not in ALLOWED_TYPES:
            type_ = "agent"
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        toolset = _scope_toolset(raw.get("toolset"), type_)
        schedule = raw.get("schedule")
        schedule = str(schedule).strip() if schedule else None
        classif_reason = str(raw.get("classif_reason") or "").strip()

        # Gate de sensibilite : un script touchant du sensible passe en a_valider.
        sensitive_reasons: list[str] = []
        statut = "actif"
        if type_ == "script":
            sensitive_reasons = classify_sensitivity(_script_command_text(payload))
            if sensitive_reasons:
                statut = "a_valider"
                classif_reason = (classif_reason + " ; script sensible : "
                                  + "; ".join(sensitive_reasons)).strip(" ;")
        if heuristic and type_ != "notification":
            # Repli degrade : on demande une validation humaine pour script/agent.
            statut = "a_valider"

        return CompiledJob(
            type=type_,
            payload=payload,
            toolset=toolset,
            classif_reason=classif_reason or f"type {type_}",
            statut=statut,
            schedule=schedule,
            sensitive_reasons=sensitive_reasons,
        )
