"""Horde auction house scanning and the item lookup table that goes with it."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import asyncpg

from . import data_tools
from .blizzard_api import BlizzardApiClient

log = logging.getLogger("discordagent.auction_house")

REGION = "us"
RULESET_ID = 1
HORDE_AUCTION_HOUSE_ID = 113

ITEM_MAX_AGE_DAYS = 7  # static item data rarely changes
ITEMS_PER_SYNC = 1000  # cap per run so a big backfill is spread out
ITEM_CONCURRENCY = 5  # 10 at once could brush Blizzard's 100 requests/second limit


async def scan_auctions(blizzard: BlizzardApiClient, pool: asyncpg.Pool) -> int:
    """Pull the Horde commodity snapshot and store it. Returns the auction count."""
    data = await blizzard.get_commodities(REGION, RULESET_ID, HORDE_AUCTION_HOUSE_ID)
    return await data_tools.save_auctions(pool, data.get("auctions") or [])


async def sync_items(blizzard: BlizzardApiClient, pool: asyncpg.Pool) -> int:
    """Look up auctioned items that are new or stale. Returns how many were saved."""
    item_ids = await data_tools.get_item_ids_to_refresh(
        pool, ITEM_MAX_AGE_DAYS, ITEMS_PER_SYNC
    )
    sem = asyncio.Semaphore(ITEM_CONCURRENCY)

    async def sync_one(item_id: int) -> bool:
        async with sem:
            try:
                data = await blizzard.get_item(REGION, item_id)
            except aiohttp.ClientResponseError as e:
                if e.status != 404:
                    log.warning("Item %s lookup failed (HTTP %s)", item_id, e.status)
                    return False
                data = {}  # Blizzard has no record; store a placeholder so we don't retry every run
            except (aiohttp.ClientError, TimeoutError):
                log.warning("Item %s lookup failed", item_id, exc_info=True)
                return False
            await data_tools.save_item(pool, item_id, data)
            return True

    results = await asyncio.gather(*(sync_one(i) for i in item_ids))
    return sum(results)
