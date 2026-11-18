"""Looking-for-group listings for WoW Forever dungeons and leveling (Postgres, schema `guild`)."""

from datetime import datetime

import asyncpg
import discord

ROLES = ("tank", "healer", "dps")
ROLE_EMOJI = {"tank": "🛡️", "healer": "💚", "dps": "⚔️"}
KINDS = ("dungeon", "leveling")
DEFAULT_ROLE_SLOTS = {"tank": 1, "healer": 1, "dps": 3}
MAX_PARTY = 5

# Typical dungeon suggestions; any text is accepted.
DUNGEON_SUGGESTIONS = [
    "Ragefire Chasm", "The Deadmines", "Wailing Caverns", "Shadowfang Keep",
    "Blackfathom Deeps", "The Stockade", "Gnomeregan", "Razorfen Kraul",
    "Scarlet Monastery", "Razorfen Downs", "Uldaman", "Zul'Farrak", "Maraudon",
    "Sunken Temple", "Blackrock Depths", "Lower Blackrock Spire", "Dire Maul",
    "Scholomance", "Stratholme",
]


class LfgError(Exception):
    """A user-facing problem, e.g. a full listing."""


def role_slots(listing: asyncpg.Record) -> dict[str, int]:
    return {"tank": listing["tanks"], "healer": listing["healers"], "dps": listing["dps"]}


async def get_linked_player(pool: asyncpg.Pool, discord_id: int) -> asyncpg.Record | None:
    """The player row, only if they have linked at least one character."""
    return await pool.fetchrow(
        """
        SELECT p.player_id::text AS player_id, p.discord_id
          FROM player p
         WHERE p.discord_id = $1
           AND EXISTS (SELECT 1 FROM "character" c WHERE c.player_id = p.player_id)
        """,
        discord_id,
    )


async def get_characters(pool: asyncpg.Pool, discord_id: int) -> list[asyncpg.Record]:
    """A player's linked characters, main first."""
    return await pool.fetch(
        """
        SELECT c.character_id::text AS character_id, c.name, c.realm_slug, c.is_main, cl.name AS class_name
          FROM "character" c
          JOIN player p ON p.player_id = c.player_id
          JOIN class cl ON cl.class_id = c.class_id
         WHERE p.discord_id = $1
         ORDER BY c.is_main DESC, c.character_id
        """,
        discord_id,
    )


async def _owned_character(conn, player_id: str, character_id: str) -> asyncpg.Record:
    char = await conn.fetchrow(
        'SELECT 1 FROM "character" WHERE character_id::text = $1 AND player_id::text = $2',
        character_id,
        player_id,
    )
    if char is None:
        raise LfgError("That character isn't linked to your account.")
    return char


async def _check_room(conn, listing: asyncpg.Record, role: str, exclude_player: str) -> None:
    """Raise LfgError if the listing can't take another member in this role."""
    signups = await conn.fetch(
        "SELECT role FROM lfg_signup WHERE listing_id = $1 AND player_id <> $2",
        listing["listing_id"],
        exclude_player,
    )
    if len(signups) >= listing["size"]:
        raise LfgError("That group is full.")
    if listing["mode"] == "roles":
        taken = sum(1 for s in signups if s["role"] == role)
        if taken >= role_slots(listing)[role]:
            raise LfgError(f"The {role} slots are already filled.")


async def create_listing(
    pool: asyncpg.Pool,
    *,
    host_id: str,
    guild_id: int,
    kind: str,
    activity: str,
    notes: str | None,
    mode: str,
    slots: dict[str, int],
    size: int,
    starts_at: datetime,
    host_character_id: str,
    host_role: str,
) -> asyncpg.Record:
    async with pool.acquire() as conn, conn.transaction():
        await _owned_character(conn, host_id, host_character_id)
        listing = await conn.fetchrow(
            """
            INSERT INTO lfg_listing
                (host_id, guild_id, kind, activity, notes, mode, tanks, healers, dps, size, starts_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
            """,
            host_id, guild_id, kind, activity, notes, mode,
            slots["tank"], slots["healer"], slots["dps"], size, starts_at,
        )
        await _check_room(conn, listing, host_role, host_id)
        await conn.execute(
            "INSERT INTO lfg_signup (listing_id, player_id, character_id, role) VALUES ($1, $2, $3, $4)",
            listing["listing_id"], host_id, host_character_id, host_role,
        )
    return listing


async def set_message(pool: asyncpg.Pool, listing_id: int, channel_id: int, message_id: int) -> None:
    await pool.execute(
        "UPDATE lfg_listing SET channel_id = $2, message_id = $3 WHERE listing_id = $1",
        listing_id, channel_id, message_id,
    )


async def get_listing(pool: asyncpg.Pool, listing_id: int) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """
        SELECT l.*, p.discord_id AS host_discord_id
          FROM lfg_listing l JOIN player p ON p.player_id::text = l.host_id
         WHERE l.listing_id = $1
        """,
        listing_id,
    )


async def get_signups(pool: asyncpg.Pool, listing_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT s.role, s.player_id, p.discord_id, c.name, c.is_main, cl.name AS class_name
          FROM lfg_signup s
          JOIN player p ON p.player_id::text = s.player_id
          JOIN "character" c ON c.character_id::text = s.character_id
          JOIN class cl ON cl.class_id = c.class_id
         WHERE s.listing_id = $1
         ORDER BY s.signed_up_at
        """,
        listing_id,
    )


async def list_open_listings(
    pool: asyncpg.Pool, guild_id: int, kind: str | None = None, limit: int = 25
) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT l.*, (SELECT count(*) FROM lfg_signup s WHERE s.listing_id = l.listing_id) AS members
          FROM lfg_listing l
         WHERE l.guild_id = $1 AND l.status = 'open' AND ($2::text IS NULL OR l.kind = $2)
         ORDER BY l.starts_at ASC NULLS LAST, l.created_at DESC
         LIMIT $3
        """,
        guild_id, kind, limit,
    )


async def sign_up(
    pool: asyncpg.Pool, listing_id: int, player_id: str, character_id: str, role: str
) -> asyncpg.Record:
    """Join (or switch character/role on) a listing. One signup per player."""
    async with pool.acquire() as conn, conn.transaction():
        listing = await conn.fetchrow(
            "SELECT * FROM lfg_listing WHERE listing_id = $1 FOR UPDATE", listing_id
        )
        if listing is None or listing["status"] != "open":
            raise LfgError("That listing isn't open.")
        await _owned_character(conn, player_id, character_id)
        await _check_room(conn, listing, role, player_id)
        await conn.execute(
            """
            INSERT INTO lfg_signup (listing_id, player_id, character_id, role)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (listing_id, player_id)
            DO UPDATE SET character_id = EXCLUDED.character_id, role = EXCLUDED.role
            """,
            listing_id, player_id, character_id, role,
        )
    return listing


async def leave(pool: asyncpg.Pool, listing_id: int, player_id: str) -> None:
    async with pool.acquire() as conn, conn.transaction():
        listing = await conn.fetchrow(
            "SELECT host_id FROM lfg_listing WHERE listing_id = $1 FOR UPDATE", listing_id
        )
        if listing is None:
            raise LfgError("That listing doesn't exist.")
        if listing["host_id"] == player_id:
            raise LfgError("You're the host. Use `/lfg close` to disband the group.")
        deleted = await conn.fetchval(
            "DELETE FROM lfg_signup WHERE listing_id = $1 AND player_id = $2 RETURNING true",
            listing_id, player_id,
        )
        if not deleted:
            raise LfgError("You aren't signed up for that listing.")


async def close_listing(pool: asyncpg.Pool, listing_id: int, player_id: str) -> asyncpg.Record:
    listing = await pool.fetchrow(
        "SELECT * FROM lfg_listing WHERE listing_id = $1", listing_id
    )
    if listing is None:
        raise LfgError("That listing doesn't exist.")
    if listing["host_id"] != player_id:
        raise LfgError("Only the host can close this listing.")
    return await pool.fetchrow(
        "UPDATE lfg_listing SET status = 'closed' WHERE listing_id = $1 RETURNING *",
        listing_id,
    )


def build_embed(listing: asyncpg.Record, signups: list[asyncpg.Record]) -> discord.Embed:
    closed = listing["status"] == "closed"
    kind = listing["kind"].capitalize()
    embed = discord.Embed(
        title=f"{'[CLOSED] ' if closed else ''}{kind}: {listing['activity']}",
        description=listing["notes"] or None,
        color=discord.Color.dark_grey() if closed else discord.Color.green(),
    )
    embed.add_field(name="Host", value=f"<@{_host_discord_id(listing, signups)}>", inline=True)
    if listing["starts_at"]:
        embed.add_field(name="Start", value=_format_start_time(listing["starts_at"]), inline=True)
    embed.add_field(
        name="Party",
        value=f"{len(signups)}/{listing['size']}"
        + (" (open signups)" if listing["mode"] == "open" else ""),
        inline=True,
    )
    for role in ROLES:
        members = [s for s in signups if s["role"] == role]
        if listing["mode"] == "roles":
            header = f"{ROLE_EMOJI[role]} {role.capitalize()} ({len(members)}/{role_slots(listing)[role]})"
        elif members:
            header = f"{ROLE_EMOJI[role]} {role.capitalize()} ({len(members)})"
        else:
            continue
        lines = [
            f"{'⭐ ' if s['is_main'] else ''}{discord.utils.escape_markdown(s['name'])} "
            f"({s['class_name']}) — <@{s['discord_id']}>"
            for s in members
        ]
        embed.add_field(name=header, value="\n".join(lines) or "—", inline=False)
    embed.set_footer(text=f"Listing #{listing['listing_id']} • ⭐ = main • /lfg join to sign up")
    return embed


def _host_discord_id(listing: asyncpg.Record, signups: list[asyncpg.Record]) -> int:
    if "host_discord_id" in listing.keys():
        return listing["host_discord_id"]
    return next(s["discord_id"] for s in signups if s["player_id"] == listing["host_id"])


def _format_start_time(starts_at: datetime) -> str:
    ts = int(starts_at.timestamp())
    return f"<t:{ts}:F> (<t:{ts}:R>)\nBe ready by start time."
