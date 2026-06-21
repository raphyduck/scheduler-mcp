"""Configuration via variables d'environnement. Aucun secret n'est logge."""

import json
import os
from dataclasses import dataclass, field

from .logging_conf import get_logger

log = get_logger("scheduler_mcp.config")


def _json_env(name: str, default):
    """Parse une variable d'environnement JSON, tolerante aux erreurs."""
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("config.json_invalide", var=name)
        return default


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    notion_token: str
    notion_version: str
    notion_programmation_ds: str
    notion_journal_db: str
    sqlite_path: str
    tick_interval_seconds: int
    notion_sync_interval_seconds: int
    max_concurrent_runs: int
    lock_ttl_seconds: int
    script_timeout_seconds: int
    llm_model: str
    llm_max_tokens: int
    log_level: str
    # Data source de la base Journal (optionnel) : si absent, le parent de page
    # utilise database_id (notion_journal_db).
    notion_journal_ds: str = ""
    # Registre des serveurs MCP du fleet : nom -> {url, authorization_token?}.
    # Alimente l'executor agent (toolset du job). Les tokens ne sont jamais logges.
    mcp_servers: dict = field(default_factory=dict)

    def public_dict(self) -> dict:
        # N'expose jamais les secrets (api keys, tokens) : seulement les noms de serveurs.
        return {
            "sqlite_path": self.sqlite_path,
            "notion_version": self.notion_version,
            "tick_interval_seconds": self.tick_interval_seconds,
            "notion_sync_interval_seconds": self.notion_sync_interval_seconds,
            "max_concurrent_runs": self.max_concurrent_runs,
            "lock_ttl_seconds": self.lock_ttl_seconds,
            "script_timeout_seconds": self.script_timeout_seconds,
            "llm_model": self.llm_model,
            "llm_max_tokens": self.llm_max_tokens,
            "log_level": self.log_level,
            "mcp_servers": sorted(self.mcp_servers),
        }


def load_config() -> Config:
    return Config(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        notion_token=os.environ.get("NOTION_TOKEN", ""),
        notion_version=os.environ.get("NOTION_VERSION", "2025-09-03"),
        notion_programmation_ds=os.environ.get("NOTION_PROGRAMMATION_DS", ""),
        notion_journal_db=os.environ.get("NOTION_JOURNAL_DB", ""),
        sqlite_path=os.environ.get("SQLITE_PATH", "/data/scheduler.db"),
        tick_interval_seconds=int(os.environ.get("TICK_INTERVAL_SECONDS", "60")),
        notion_sync_interval_seconds=int(os.environ.get("NOTION_SYNC_INTERVAL_SECONDS", "300")),
        max_concurrent_runs=int(os.environ.get("MAX_CONCURRENT_RUNS", "4")),
        lock_ttl_seconds=int(os.environ.get("LOCK_TTL_SECONDS", "900")),
        script_timeout_seconds=int(os.environ.get("SCRIPT_TIMEOUT_SECONDS", "300")),
        llm_model=os.environ.get("LLM_MODEL", "claude-haiku-4-5"),
        llm_max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "4096")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        notion_journal_ds=os.environ.get("NOTION_JOURNAL_DS", ""),
        mcp_servers=_json_env("MCP_SERVERS", {}),
    )
