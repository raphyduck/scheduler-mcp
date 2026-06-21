"""Tests de l'auth machine MCP (BUILD_BRIEF.md commit 11).

Autonome : faux client HTTP et horloge injectable (aucun reseau).
    python -m tests.test_auth
Verifie le token seede statique, le mode proxy (recuperation, mise en cache,
rafraichissement avant expiration) et la resilience aux erreurs.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from scheduler_mcp.auth import MachineAuth
from scheduler_mcp.config import Config


def make_cfg(token="", proxy="", refresh_days=180) -> Config:
    return Config(
        anthropic_api_key="", notion_token="", notion_version="2025-09-03",
        notion_programmation_ds="", notion_journal_db="", sqlite_path=":memory:",
        tick_interval_seconds=60, notion_sync_interval_seconds=300,
        max_concurrent_runs=4, lock_ttl_seconds=900, script_timeout_seconds=300,
        llm_model="x", llm_max_tokens=4096, log_level="INFO",
        mcp_auth_token=token, mcp_oauth_proxy_url=proxy,
        mcp_auth_refresh_days=refresh_days, mcp_servers={},
    )


class FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class FakeHttp:
    def __init__(self, data):
        self._data = data
        self.calls = 0

    async def post(self, url, headers=None):
        self.calls += 1
        self.last_headers = headers
        return FakeResp(self._data)


class RaisingHttp:
    def __init__(self):
        self.calls = 0

    async def post(self, url, headers=None):
        self.calls += 1
        raise RuntimeError("proxy down")


class Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t


def run(coro):
    return asyncio.run(coro)


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_token_statique() -> None:
    auth = MachineAuth(make_cfg(token="seed-tok"))
    assert auth.configured is True
    assert run(auth.token()) == "seed-tok"


def test_non_configure() -> None:
    auth = MachineAuth(make_cfg())
    assert auth.configured is False
    assert run(auth.token()) is None


def test_proxy_recupere_et_cache() -> None:
    http = FakeHttp({"access_token": "abc", "expires_in": 3600})
    clock = Clock(T0)
    auth = MachineAuth(make_cfg(token="seed", proxy="https://proxy"), client=http, now=clock)
    assert run(auth.token()) == "abc"
    assert http.calls == 1
    # Le token seede part en Bearer vers le proxy.
    assert http.last_headers["Authorization"] == "Bearer seed"
    # Toujours valide -> cache, pas de second appel.
    clock.t = T0 + timedelta(seconds=100)
    assert run(auth.token()) == "abc"
    assert http.calls == 1


def test_proxy_rafraichit_avant_expiration() -> None:
    http = FakeHttp({"access_token": "abc", "expires_in": 3600})
    clock = Clock(T0)
    auth = MachineAuth(make_cfg(proxy="https://proxy"), client=http, now=clock)
    run(auth.token())
    assert http.calls == 1
    # Au-dela de la fenetre de refresh (3600 - marge), nouvel appel.
    clock.t = T0 + timedelta(seconds=4000)
    run(auth.token())
    assert http.calls == 2


def test_proxy_expires_at_absolu() -> None:
    exp = (T0 + timedelta(days=180)).isoformat()
    http = FakeHttp({"token": "xyz", "expires_at": exp})
    auth = MachineAuth(make_cfg(proxy="https://proxy"), client=http, now=Clock(T0))
    assert run(auth.token()) == "xyz"


def test_proxy_erreur_resiliente() -> None:
    http = RaisingHttp()
    auth = MachineAuth(make_cfg(proxy="https://proxy"), client=http, now=Clock(T0))
    # Erreur proxy -> pas de token, mais pas de crash.
    assert run(auth.token()) is None
    assert http.calls == 1


def main() -> None:
    for test in [
        test_token_statique,
        test_non_configure,
        test_proxy_recupere_et_cache,
        test_proxy_rafraichit_avant_expiration,
        test_proxy_expires_at_absolu,
        test_proxy_erreur_resiliente,
    ]:
        test()
    print("OK : tous les tests de l'auth machine passent")


if __name__ == "__main__":
    main()
