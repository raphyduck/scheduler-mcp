"""Executor script (BUILD_BRIEF.md commit 6).

Execute un script deterministe dans un subprocess isole : capture stdout, stderr
et code retour, avec timeout et environnement reduit (least privilege, le process
n'herite pas des secrets du parent). Zero LLM.

Gate de sensibilite : classify_sensitivity detecte les operations a risque
(suppression, acces credentials, envoi externe). Le compiler (commit 8) s'en sert
pour creer en statut a_valider tout script sensible, qui n'atteint l'executor
qu'une fois valide (passe en actif). L'executor journalise la sensibilite pour
l'audit et peut bloquer durement si block_sensitive est actif.
"""

import asyncio
import json
import os
import re
import shlex
import signal
from dataclasses import dataclass, field
from typing import Optional

from ..logging_conf import get_logger
from .base import RunResult

log = get_logger("scheduler_mcp.executors.script")

# Taille max de stdout/stderr conservee dans le detail du run.
_OUTPUT_LIMIT = 4000

# Motifs d'operations sensibles -> raison lisible. Recherche insensible a la casse.
_SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+(-[a-z]*\s+)*-?[a-z]*f", "suppression de fichiers (rm -f)"),
    (r"\b(rmdir|unlink|shred|mkfs|fdisk)\b", "operation destructive sur le disque"),
    (r"\bdd\b.*\bof=", "ecriture brute (dd)"),
    (r"\b(drop|truncate|delete)\s+(table|from|database)\b", "operation SQL destructive"),
    (r"(\.env\b|id_rsa|\.ssh/|secret|credential|password|passwd|token|api[_-]?key)",
     "acces a des secrets ou credentials"),
    (r"\b(bw|bitwarden)\b", "acces au coffre Bitwarden"),
    (r"\b(curl|wget|scp|sftp|rsync|nc|netcat|ftp)\b", "transfert reseau externe"),
    (r"https?://", "acces reseau externe"),
    (r"\b(sendmail|mailx|mutt)\b", "envoi d'email"),
    (r">\s*/(etc|boot|sys|dev/sd)", "ecriture dans un emplacement systeme"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "arret/redemarrage de la machine"),
]


def classify_sensitivity(command_text: str) -> list[str]:
    """Retourne les raisons de sensibilite detectees (liste vide si benin)."""
    reasons: list[str] = []
    for pattern, reason in _SENSITIVE_PATTERNS:
        if re.search(pattern, command_text, re.IGNORECASE) and reason not in reasons:
            reasons.append(reason)
    return reasons


@dataclass
class ScriptSpec:
    """Specification d'execution projetee depuis le payload du job."""

    args: list[str]
    command_text: str
    shell: bool
    cwd: Optional[str] = None
    env: dict = field(default_factory=dict)
    timeout: Optional[int] = None


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


def parse_script_spec(payload: dict) -> ScriptSpec:
    """Projette le payload en ScriptSpec, ou leve ValueError si invalide.

    Accepte soit args (argv en liste, recommande pour l'isolation), soit command
    (chaine ; split par shlex en mode non-shell, ou passee au shell si shell=true).
    """
    shell = bool(payload.get("shell", False))
    env = payload.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError("env doit etre un objet cle/valeur")
    cwd = payload.get("cwd")
    timeout = payload.get("timeout", payload.get("timeout_seconds"))

    args_value = payload.get("args")
    command_value = payload.get("command")

    if args_value:
        if not isinstance(args_value, list) or not all(isinstance(a, str) for a in args_value):
            raise ValueError("args doit etre une liste de chaines")
        args = args_value
        command_text = " ".join(args)
    elif isinstance(command_value, str) and command_value.strip():
        command_text = command_value
        if shell:
            args = [command_value]
        else:
            args = shlex.split(command_value)
            if not args:
                raise ValueError("command vide apres decoupage")
    else:
        raise ValueError("payload script sans args ni command")

    return ScriptSpec(
        args=args,
        command_text=command_text,
        shell=shell,
        cwd=cwd,
        env={str(k): str(v) for k, v in env.items()},
        timeout=int(timeout) if timeout is not None else None,
    )


def _truncate(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(text) > _OUTPUT_LIMIT:
        return text[:_OUTPUT_LIMIT] + f"\n... ({len(text) - _OUTPUT_LIMIT} caracteres tronques)"
    return text


class ScriptExecutor:
    """Executor pour les jobs de type script."""

    def __init__(self, default_timeout: int = 300, block_sensitive: bool = False) -> None:
        self._default_timeout = default_timeout
        self._block_sensitive = block_sensitive

    def _build_env(self, spec: ScriptSpec) -> dict:
        # Environnement reduit : le script n'herite pas des secrets du parent.
        base = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
        base.update(spec.env)
        return base

    async def execute(self, job: dict) -> RunResult:
        payload = _coerce_payload(job.get("payload"))
        if payload is None:
            return RunResult.fail("payload de script vide ou non JSON")

        try:
            spec = parse_script_spec(payload)
        except ValueError as exc:
            return RunResult.fail(str(exc))

        reasons = classify_sensitivity(spec.command_text)
        if reasons:
            log.warning("script.sensible", job=job.get("id"), raisons=reasons)
            if self._block_sensitive:
                return RunResult.skip(
                    "script sensible bloque (block_sensitive) : " + "; ".join(reasons)
                )

        timeout = spec.timeout if spec.timeout is not None else self._default_timeout
        return await self._run(spec, timeout, reasons)

    async def _run(self, spec: ScriptSpec, timeout: int, reasons: list[str]) -> RunResult:
        env = self._build_env(spec)
        try:
            if spec.shell:
                proc = await asyncio.create_subprocess_shell(
                    spec.command_text,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=spec.cwd,
                    env=env,
                    start_new_session=True,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *spec.args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=spec.cwd,
                    env=env,
                    start_new_session=True,
                )
        except FileNotFoundError:
            return RunResult.fail(f"commande introuvable: {spec.args[0]}")
        except OSError as exc:
            return RunResult.fail(f"lancement impossible: {exc}")

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            self._kill(proc)
            await proc.wait()
            return RunResult.fail(f"timeout apres {timeout}s")

        rc = proc.returncode
        detail = self._format_detail(rc, stdout, stderr, reasons)
        if rc == 0:
            return RunResult.ok(detail)
        return RunResult.fail(detail)

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        # Tue tout le groupe de process (start_new_session) pour ne rien laisser orphelin.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    @staticmethod
    def _format_detail(rc, stdout: bytes, stderr: bytes, reasons: list[str]) -> str:
        parts = [f"code retour: {rc}"]
        if reasons:
            parts.append("sensibilite: " + "; ".join(reasons))
        parts.append("stdout:\n" + _truncate(stdout))
        parts.append("stderr:\n" + _truncate(stderr))
        return "\n".join(parts)
