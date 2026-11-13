"""Database access for linked players and their WoW characters (Postgres, schema `guild`)."""

import json
import logging

import asyncpg

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS guild;
SET search_path TO guild;

CREATE TABLE IF NOT EXISTS class (
    class_id integer PRIMARY KEY,
    name     text NOT NULL
);

INSERT INTO class (class_id, name) VALUES
    (1, 'Warrior'), (2, 'Paladin'), (3, 'Hunter'), (4, 'Rogue'), (5, 'Priest'),
    (6, 'Death Knight'), (7, 'Shaman'), (8, 'Mage'), (9, 'Warlock'), (10, 'Monk'),
    (11, 'Druid'), (12, 'Demon Hunter'), (13, 'Evoker')
ON CONFLICT (class_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS player (
    player_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    discord_id      bigint NOT NULL UNIQUE,
    bnet_account_id bigint UNIQUE,
    battletag       text,
    consent_version text,
    consented_at    timestamptz
);

CREATE TABLE IF NOT EXISTS "character" (
    character_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    blizzard_character_id bigint UNIQUE,
    player_id             bigint NOT NULL REFERENCES player ON DELETE CASCADE,
    region                text NOT NULL,
    realm_slug            text NOT NULL,
    name                  text NOT NULL,
    class_id              integer NOT NULL REFERENCES class,
    is_main               boolean NOT NULL DEFAULT false,
    verified              boolean NOT NULL DEFAULT false,
    UNIQUE (region, realm_slug, name)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_main_per_player
    ON "character" (player_id) WHERE is_main;

-- One row per equipped slot; replaced wholesale on each refresh.
CREATE TABLE IF NOT EXISTS equipment (
    character_id bigint NOT NULL REFERENCES "character" ON DELETE CASCADE,
    slot         text NOT NULL,     -- Blizzard slot type, e.g. HEAD, MAIN_HAND
    item_id      bigint,
    name         text,
    item_level   integer,
    quality      text,              -- e.g. EPIC, RARE
    raw          jsonb NOT NULL,    -- the full equipped_items entry from Blizzard
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (character_id, slot)
);

-- Current Horde commodity auctions; rows for auctions no longer listed are removed on each scan.
CREATE TABLE IF NOT EXISTS auction (
    auction_id bigint PRIMARY KEY,
    item_id    bigint NOT NULL,
    quantity   integer NOT NULL,
    unit_price bigint,              -- copper per unit
    time_left  text NOT NULL        -- SHORT, MEDIUM, LONG, VERY_LONG
);
-- The item_id index lives in sql/create_auction_tables.sql: CREATE INDEX needs table
-- ownership (even with IF NOT EXISTS), and the bot's role doesn't own these tables.

-- Static item data, looked up for items seen on the auction house and refreshed rarely.
CREATE TABLE IF NOT EXISTS item (
    item_id       bigint PRIMARY KEY,
    name          text,             -- NULL if Blizzard has no data for this item
    quality       text,             -- e.g. COMMON, RARE, EPIC
    item_class    text,
    item_subclass text,
    sell_price     bigint,          -- copper a vendor pays you for one
    purchase_price bigint,          -- copper a vendor charges for one
    raw           jsonb NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS oauth_link_state (
    state           text PRIMARY KEY,
    discord_id      bigint NOT NULL,
    expires_at      timestamptz NOT NULL DEFAULT now() + interval '10 minutes',
    consent_version text,
    consented_at    timestamptz
);
"""

UPSERT_PLAYER = """
INSERT INTO player (discord_id, bnet_account_id, battletag, consent_version, consented_at)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (discord_id) DO UPDATE
   SET bnet_account_id = EXCLUDED.bnet_account_id,
       battletag       = EXCLUDED.battletag,
       consent_version = EXCLUDED.consent_version,
       consented_at    = EXCLUDED.consented_at
RETURNING player_id"""

# A verified link wins over an unverified claim by someone else; a character that
# changes owner loses its "main" flag so the one-main-per-player index holds.
UPDATE_CHARACTER = """
UPDATE "character"
   SET is_main = (player_id = $2) AND is_main,
       player_id = $2, region = $3, realm_slug = $4, name = $5, class_id = $6, verified = true
 WHERE blizzard_character_id = $1"""

INSERT_CHARACTER = """
INSERT INTO "character" (blizzard_character_id, player_id, region, realm_slug, name, class_id, verified)
VALUES ($1, $2, $3, $4, $5, $6, true)
ON CONFLICT (region, realm_slug, name) DO UPDATE
   SET is_main = ("character".player_id = EXCLUDED.player_id) AND "character".is_main,
       player_id = EXCLUDED.player_id,
       blizzard_character_id = EXCLUDED.blizzard_character_id,
       class_id = EXCLUDED.class_id,
       verified = true"""


INSERT_EQUIPMENT = """
INSERT INTO equipment (character_id, slot, item_id, name, item_level, quality, raw)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)"""


async def create_pool(dsn: str) -> asyncpg.Pool:
    """Create the pool, ensure the schema exists, and default to the `guild` schema."""
    pool = await asyncpg.create_pool(
        dsn,
        ssl=False,
        server_settings={"search_path": "guild"},
    )
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
    return pool


async def create_link_state(pool: asyncpg.Pool, state: str, discord_id: int) -> None:
    await pool.execute(
        "INSERT INTO oauth_link_state (state, discord_id) VALUES ($1, $2)",
        state,
        discord_id,
    )


async def is_state_valid(pool: asyncpg.Pool, state: str) -> bool:
    return bool(
        await pool.fetchval(
            "SELECT true FROM oauth_link_state WHERE state = $1 AND expires_at > now()",
            state,
        )
    )


async def record_consent(pool: asyncpg.Pool, state: str, consent_version: str) -> bool:
    return bool(
        await pool.fetchval(
            """UPDATE oauth_link_state
                  SET consent_version = $2, consented_at = now()
                WHERE state = $1 AND expires_at > now()
            RETURNING true""",
            state,
            consent_version,
        )
    )


async def claim_state(pool: asyncpg.Pool, state: str) -> asyncpg.Record | None:
    """Atomically consume a consented state so it works exactly once."""
    return await pool.fetchrow(
        """DELETE FROM oauth_link_state
            WHERE state = $1 AND expires_at > now() AND consented_at IS NOT NULL
        RETURNING discord_id, consent_version, consented_at""",
        state,
    )


async def purge_expired_states(pool: asyncpg.Pool) -> None:
    await pool.execute("DELETE FROM oauth_link_state WHERE expires_at < now()")


async def save_link(
    pool: asyncpg.Pool,
    link: asyncpg.Record,
    account: dict,
    characters: list[dict],
    region: str,
) -> bool:
    """Save the player and characters. Returns False if the Battle.net account belongs to someone else."""
    async with pool.acquire() as conn, conn.transaction():
        owner = await conn.fetchval(
            "SELECT discord_id FROM player WHERE bnet_account_id = $1", account["id"]
        )
        if owner is not None and owner != link["discord_id"]:
            return False

        player_id = await conn.fetchval(
            UPSERT_PLAYER,
            link["discord_id"],
            account["id"],
            account.get("battletag"),
            link["consent_version"],
            link["consented_at"],
        )
        for c in characters:
            realm_slug = c.get("realm", {}).get("slug", "")
            name = c.get("name", "N/A") + " " + c.get("surname", "")
            args = (
                c["id"],
                player_id,
                region,
                realm_slug,
                name,
                c["playable_class"]["id"],
            )
            # Match on Blizzard's ID first so renames and realm transfers update in place.
            if await conn.execute(UPDATE_CHARACTER, *args) == "UPDATE 0":
                await conn.execute(INSERT_CHARACTER, *args)
    return True


async def delete_player(pool: asyncpg.Pool, discord_id: int) -> bool:
    """Delete a player and (via cascade) all of their characters."""
    return bool(
        await pool.fetchval(
            "DELETE FROM player WHERE discord_id = $1 RETURNING true", discord_id
        )
    )


async def get_player_characters(
    pool: asyncpg.Pool, discord_id: int
) -> list[asyncpg.Record]:
    """All of a player's linked characters."""
    return await pool.fetch(
        """
        SELECT c.character_id, c.region, c.realm_slug, c.name
          FROM "character" c
          JOIN player p ON p.player_id = c.player_id
         WHERE p.discord_id = $1
         ORDER BY c.character_id
        """,
        discord_id,
    )


async def get_all_characters(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Every linked character, for background refreshes."""
    return await pool.fetch(
        'SELECT character_id, region, realm_slug, name FROM "character" ORDER BY character_id'
    )


def _equipment_rows(character_id: int, equipment: dict) -> list[tuple]:
    """Flatten Blizzard's equipment response into INSERT_EQUIPMENT rows, one per slot."""
    by_slot: dict[str, tuple] = {}
    for item in equipment.get("equipped_items") or []:
        slot = (item.get("slot") or {}).get("type")
        if not slot:
            continue
        by_slot[slot] = (
            character_id,
            slot,
            (item.get("item") or {}).get("id"),
            item.get("name"),
            (item.get("level") or {}).get("value"),
            (item.get("quality") or {}).get("type"),
            json.dumps(item),
        )
    return list(by_slot.values())


async def save_equipment(pool: asyncpg.Pool, character_id: int, equipment: dict) -> int:
    """Replace a character's stored gear with a Blizzard equipment response. Returns the slot count."""
    rows = _equipment_rows(character_id, equipment)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM equipment WHERE character_id = $1", character_id)
        if rows:
            await conn.executemany(INSERT_EQUIPMENT, rows)
    return len(rows)


AUCTION_COLUMNS = ("auction_id", "item_id", "quantity", "unit_price", "time_left")


def _auction_rows(auctions: list[dict]) -> list[tuple]:
    """Flatten Blizzard's auctions into rows; later duplicates of an id win."""
    by_id: dict[int, tuple] = {}
    for a in auctions:
        item_id = (a.get("item") or {}).get("id")
        if a.get("id") is None or item_id is None:
            continue
        by_id[a["id"]] = (
            a["id"],
            item_id,
            a.get("quantity", 0),
            a.get("unit_price"),
            a.get("time_left", ""),
        )
    return list(by_id.values())


async def save_auctions(pool: asyncpg.Pool, auctions: list[dict]) -> int:
    """Upsert a full auction snapshot and drop auctions no longer listed. Returns the row count."""
    rows = _auction_rows(auctions)
    async with pool.acquire() as conn, conn.transaction():
        # Bulk-load into a temp table with COPY; row-by-row inserts are far too slow here.
        await conn.execute(
            "CREATE TEMP TABLE auction_snapshot (LIKE auction) ON COMMIT DROP"
        )
        await conn.copy_records_to_table(
            "auction_snapshot", records=rows, columns=AUCTION_COLUMNS
        )
        await conn.execute(
            """
            INSERT INTO auction (auction_id, item_id, quantity, unit_price, time_left)
            SELECT auction_id, item_id, quantity, unit_price, time_left FROM auction_snapshot
            ON CONFLICT (auction_id) DO UPDATE
               SET item_id    = EXCLUDED.item_id,
                   quantity   = EXCLUDED.quantity,
                   unit_price = EXCLUDED.unit_price,
                   time_left  = EXCLUDED.time_left
            """
        )
        await conn.execute(
            """
            DELETE FROM auction a
             WHERE NOT EXISTS (SELECT 1 FROM auction_snapshot s WHERE s.auction_id = a.auction_id)
            """
        )
    return len(rows)


async def get_item_ids_to_refresh(
    pool: asyncpg.Pool, max_age_days: int, limit: int
) -> list[int]:
    """Auctioned items with no lookup row yet (first), then ones older than max_age_days."""
    return [
        r["item_id"]
        for r in await pool.fetch(
            """
            SELECT a.item_id
              FROM (SELECT DISTINCT item_id FROM auction) a
              LEFT JOIN item i ON i.item_id = a.item_id
             WHERE i.item_id IS NULL
                OR i.updated_at < now() - make_interval(days => $1)
             ORDER BY i.updated_at NULLS FIRST
             LIMIT $2
            """,
            max_age_days,
            limit,
        )
    ]


async def save_item(pool: asyncpg.Pool, item_id: int, data: dict) -> None:
    """Upsert one item. An empty dict records 'no data' so it isn't re-fetched until stale."""
    await pool.execute(
        """
        INSERT INTO item (item_id, name, quality, item_class, item_subclass,
                          sell_price, purchase_price, raw, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, now())
        ON CONFLICT (item_id) DO UPDATE
           SET name           = EXCLUDED.name,
               quality        = EXCLUDED.quality,
               item_class     = EXCLUDED.item_class,
               item_subclass  = EXCLUDED.item_subclass,
               sell_price     = EXCLUDED.sell_price,
               purchase_price = EXCLUDED.purchase_price,
               raw            = EXCLUDED.raw,
               updated_at     = now()
        """,
        item_id,
        data.get("name"),
        (data.get("quality") or {}).get("type"),
        (data.get("item_class") or {}).get("name"),
        (data.get("item_subclass") or {}).get("name"),
        data.get("sell_price"),
        data.get("purchase_price"),
        json.dumps(data),
    )


def _like_escape(text: str) -> str:
    """Escape LIKE wildcards so user input matches literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_auctioned_items(
    pool: asyncpg.Pool, query: str, limit: int = 25
) -> list[asyncpg.Record]:
    """Items currently on the auction house whose name contains `query`, best matches first."""
    return await pool.fetch(
        """
        SELECT i.item_id, i.name
          FROM item i
         WHERE i.name ILIKE '%' || $1 || '%'
           AND EXISTS (SELECT 1 FROM auction a WHERE a.item_id = i.item_id)
         ORDER BY lower(i.name) = lower($2) DESC,   -- exact match
                  i.name ILIKE $1 || '%' DESC,       -- then prefix match
                  ' ' || i.name ILIKE '% ' || $1 || '%' DESC,  -- then a word starts with it
                  length(i.name), i.name
         LIMIT $3
        """,
        _like_escape(query),
        query,
        limit,
    )


async def find_item(pool: asyncpg.Pool, text: str) -> asyncpg.Record | None:
    """Resolve an item ID or name to (item_id, name), preferring items that are listed."""
    text = text.strip()
    if text.isdigit():
        return await pool.fetchrow(
            """SELECT a.item_id, i.name
                 FROM (SELECT $1::bigint AS item_id) a
                 LEFT JOIN item i ON i.item_id = a.item_id""",
            int(text),
        )
    if not text:
        return None
    listed = await search_auctioned_items(pool, text, limit=1)
    if listed:
        return listed[0]
    # Not on the AH right now, but say so by name if we know the item.
    return await pool.fetchrow(
        "SELECT item_id, name FROM item WHERE name ILIKE $1 ORDER BY item_id LIMIT 1",
        _like_escape(text),
    )


async def get_auction_price(pool: asyncpg.Pool, item_id: int) -> asyncpg.Record:
    """Price summary for one item across current listings (counts are 0 if none)."""
    return await pool.fetchrow(
        """
        SELECT min(a.unit_price)                                    AS min_price,
               (sum(a.unit_price * a.quantity) / NULLIF(sum(a.quantity), 0))::bigint
                                                                    AS avg_price,
               coalesce(sum(a.quantity), 0)                         AS quantity,
               count(a.auction_id)                                  AS auctions,
               (SELECT sell_price FROM item WHERE item_id = $1)     AS sell_price
          FROM auction a
         WHERE a.item_id = $1 AND a.unit_price IS NOT NULL
        """,
        item_id,
    )


async def get_player_main_or_first_character(
    pool: asyncpg.Pool, discord_id: int
) -> asyncpg.Record | None:
    """Return the player's main character, or the first linked one if no main is set."""
    return await pool.fetchrow(
        """
        SELECT c.character_id, c.region, c.realm_slug, c.name
          FROM "character" c
          JOIN player p ON p.player_id = c.player_id
         WHERE p.discord_id = $1
         ORDER BY c.is_main DESC, c.character_id ASC
         LIMIT 1
        """,
        discord_id,
    )
