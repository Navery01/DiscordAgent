"""Blizzard API client using OAuth client credentials."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

import aiohttp

OAUTH_BASE = "https://oauth.battle.net"


class BlizzardApiClient:
    """Small wrapper around Blizzard OAuth + profile APIs with token caching."""

    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.close()

    async def _get_access_token(self, force_refresh: bool = False) -> str:
        now = time.monotonic()
        if not force_refresh and self._token and now < self._expires_at:
            return self._token

        async with self._token_lock:
            now = time.monotonic()
            if not force_refresh and self._token and now < self._expires_at:
                return self._token

            async with self._http.post(
                f"{OAUTH_BASE}/token",
                auth=aiohttp.BasicAuth(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()

            self._token = payload["access_token"]
            # Refresh one minute early so active requests don't race token expiry.
            self._expires_at = time.monotonic() + max(
                int(payload["expires_in"]) - 60, 1
            )
            return self._token

    async def get_profile_json(
        self,
        region: str,
        path: str,
        *,
        namespace: str | None = None,
        locale: str = "en_US",
    ) -> dict:
        """Call a Blizzard profile endpoint and return JSON."""
        namespace = namespace or f"profile-{region}"
        url = f"https://{region}.api.blizzard.com{path}"
        params = {"namespace": namespace, "locale": locale}

        token = await self._get_access_token()
        for attempt in (1, 2):
            async with self._http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status == 401 and attempt == 1:
                    token = await self._get_access_token(force_refresh=True)
                    continue
                resp.raise_for_status()
                return await resp.json()

        raise RuntimeError("unreachable")

    async def get_character_summary(
        self, region: str, realm_slug: str, character_name: str
    ) -> dict:
        """Fetch a single character summary."""
        safe_realm = quote(realm_slug, safe="")
        safe_name = quote(character_name.lower(), safe="")
        return await self.get_profile_json(
            region,
            f"/profile/wow/character/{safe_realm}/{safe_name}",
        )
