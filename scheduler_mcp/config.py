"""Configuration via variables d'environnement. Aucun secret n'est logge."""

import os
from dataclasses import dataclass


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
    llm_model: str
    log_level: str

    def public_dict(self) -> dict:
        # N'expose jamais les secrets (api keys, tokens).
        return {
            "sqlite_path": self.sqlite_path,
            "notion_version": self.notion_version,
            "tick_interval_seconds": self.tick_interval_seconds,
            "notion_sync_interval_seconds": self.notion_sync_interval_seconds,
            "max_concurrent_runs": self.max_concurrent_runs,
            "llm_model": self.llm_model,
            "log_level": self.log_level,
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
        llm_model=os.environ.get("LLM_MODEL", "claude-haiku-4-5"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
