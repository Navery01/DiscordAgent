"""Blizzard API client using OAuth client credentials."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from urllib.parse import quote

import aiohttp

log = logging.getLogger("discordagent.blizzard_api")

OAUTH_BASE = "https://oauth.battle.net"

# 429 = rate limited; 5xx = Blizzard having a moment. Both are worth retrying.
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
BACKOFF_BASE = 1.0  # seconds; doubles each retry: 1, 2, 4, 8, 16
BACKOFF_MAX = 30.0


def _backoff_delay(attempt: int, retry_after: str | None = None) -> float:
    """Seconds to wait before retry number `attempt` (0-based).

    Honors a numeric Retry-After header when Blizzard sends one; otherwise
    exponential backoff with jitter so concurrent requests don't retry in lockstep.
    """
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), BACKOFF_MAX)
        except ValueError:
            pass  # an HTTP-date; fall back to our own schedule
    delay = min(BACKOFF_BASE * 2**attempt, BACKOFF_MAX)
    return delay / 2 + random.uniform(0, delay / 2)


def character_slug(name: str) -> str:
    """Blizzard's URL slug for a character name: 'Foo Bar ' -> 'foo-bar'."""
    return quote("-".join(name.lower().split()), safe="")


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
        timeout: float | None = None,
    ) -> dict:
        """Call a Blizzard API endpoint and return JSON (profile namespace by default)."""
        namespace = namespace or f"profile-forever-{region}"
        url = f"https://{region}.api.blizzard.com{path}"
        params = {"namespace": namespace, "locale": locale}
        # None keeps the session's 10s default; big payloads pass a longer one.
        request_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else None

        token = await self._get_access_token()
        token_refreshed = False
        retries = 0
        while True:
            try:
                async with self._http.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=request_timeout,
                ) as resp:
                    if resp.status == 401 and not token_refreshed:
                        token_refreshed = True
                        token = await self._get_access_token(force_refresh=True)
                        continue
                    if resp.status not in RETRY_STATUSES or retries >= MAX_RETRIES:
                        resp.raise_for_status()
                        return await resp.json()
                    delay = _backoff_delay(retries, resp.headers.get("Retry-After"))
                    reason = f"HTTP {resp.status}"
            except (aiohttp.ClientConnectionError, TimeoutError) as e:
                if retries >= MAX_RETRIES:
                    raise
                delay = _backoff_delay(retries)
                reason = type(e).__name__

            retries += 1
            log.debug(
                "%s from %s; retry %d/%d in %.1fs", reason, path, retries, MAX_RETRIES, delay
            )
            await asyncio.sleep(delay)

    async def get_character_summary(
        self, region: str, realm_slug: str, character_name: str
    ) -> dict:
        """Fetch a single character summary."""
        safe_realm = quote(realm_slug, safe="")
        return await self.get_profile_json(
            region,
            f"/profile/wow/character/{safe_realm}/{character_slug(character_name)}",
        )

    async def get_character_equipment(self, region: str, character_name: str) -> dict:
        """Fetch a character's equipped items."""
        return await self.get_profile_json(
            region,
            f"/profile/wow/character/{character_slug(character_name)}/equipment",
        )

    async def get_commodities(self, region: str, ruleset_id: int, auction_house_id: int) -> dict:
        """Fetch the current commodity auctions for one auction house (refreshed hourly by Blizzard)."""
        return await self.get_profile_json(
            region,
            f"/data/wow/ruleset/{ruleset_id}/auctions/{auction_house_id}/commodities",
            namespace=f"dynamic-forever-{region}",
            timeout=120,  # the full snapshot can be many MB
        )

    async def get_item(self, region: str, item_id: int) -> dict:
        """Fetch static item data (name, quality, class...)."""
        return await self.get_profile_json(
            region,
            f"/data/wow/item/{int(item_id)}",
            namespace=f"static-forever-{region}",
        )
