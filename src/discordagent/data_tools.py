"""Database access for linked players and their WoW characters (Postgres, schema `guild`)."""

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
            args = (
                c["id"],
                player_id,
                region,
                c["realm"]["slug"],
                c["name"],
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


async def get_player_main_or_first_character(
    pool: asyncpg.Pool, discord_id: int
) -> asyncpg.Record | None:
    """Return the player's main character, or the first linked one if no main is set."""
    return await pool.fetchrow(
        """
        SELECT c.region, c.realm_slug, c.name
          FROM "character" c
          JOIN player p ON p.player_id = c.player_id
         WHERE p.discord_id = $1
         ORDER BY c.is_main DESC, c.character_id ASC
         LIMIT 1
        """,
        discord_id,
    )
