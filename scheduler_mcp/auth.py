"""Auth machine MCP (BUILD_BRIEF.md commit 11).

Decision retenue : connecteur MCP natif de l'API Messages (deja en place au
commit 7). Ce module ne fait que fournir le token machine a injecter dans les
serveurs MCP du fleet, pour que l'executor agent s'authentifie sans interaction.

Deux modes :
- token long-lived seede (MCP_AUTH_TOKEN) : injecte tel quel ;
- proxy mcp-oauth (MCP_OAUTH_PROXY_URL) : le token est recupere puis rafraichi
  avant expiration. Le refresh effectif (cadence ~180j) est gere cote proxy ;
  ici on remplace le token en cache avant qu'il n'expire.

Le contrat HTTP du proxy (POST avec le token seede en Bearer, reponse JSON
access_token + expiry) est minimal et a confirmer au branchement. Aucun secret
n'est logge.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import httpx

from .config import Config
from .logging_conf import get_logger

log = get_logger("scheduler_mcp.auth")

# Plafond de marge de rafraichissement avant expiration (1 jour).
_MAX_MARGIN_SECONDS = 86400


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MachineAuth:
    """Resout le token machine a injecter dans les serveurs MCP."""

    def __init__(
        self,
        cfg: Config,
        client: Any = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._now = now or _utcnow
        self._token: Optional[str] = None
        self._refresh_at: Optional[datetime] = None

    @property
    def configured(self) -> bool:
        return bool(self._cfg.mcp_auth_token or self._cfg.mcp_oauth_proxy_url)

    async def token(self) -> Optional[str]:
        """Token bearer courant, ou None si l'auth machine n'est pas configuree."""
        if self._cfg.mcp_oauth_proxy_url:
            return await self._proxy_token()
        return self._cfg.mcp_auth_token or None

    async def _proxy_token(self) -> Optional[str]:
        if self._token is not None and not self._needs_refresh():
            return self._token
        await self._refresh()
        return self._token

    def _needs_refresh(self) -> bool:
        return self._refresh_at is None or self._now() >= self._refresh_at

    async def _refresh(self) -> None:
        now = self._now()
        try:
            data = await self._fetch()
        except Exception as exc:
            log.error("auth.refresh_echec", error=str(exc), type=type(exc).__name__)
            return  # on conserve l'ancien token si possible
        token = data.get("access_token") or data.get("token")
        if not token:
            log.error("auth.reponse_sans_token")
            return
        self._token = token
        self._schedule_refresh(now, data)
        log.info("auth.token_rafraichi", refresh_at=self._refresh_at.isoformat())

    def _schedule_refresh(self, now: datetime, data: dict) -> None:
        ttl_seconds = self._ttl_seconds(now, data)
        margin = min(_MAX_MARGIN_SECONDS, max(0, ttl_seconds * 0.1))
        self._refresh_at = now + timedelta(seconds=max(0, ttl_seconds - margin))

    def _ttl_seconds(self, now: datetime, data: dict) -> float:
        expires_at = data.get("expires_at")
        if expires_at:
            try:
                exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                return max(0.0, (exp - now).total_seconds())
            except ValueError:
                pass
        if data.get("expires_in") is not None:
            return float(data["expires_in"])
        return float(self._cfg.mcp_auth_refresh_days) * 86400.0

    async def _fetch(self) -> dict:
        url = self._cfg.mcp_oauth_proxy_url
        headers = {}
        if self._cfg.mcp_auth_token:
            headers["Authorization"] = f"Bearer {self._cfg.mcp_auth_token}"
        if self._client is not None:
            resp = await self._client.post(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
