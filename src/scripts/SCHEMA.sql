-- =====================================================================
-- Guild Bot — OLTP schema (PostgreSQL 14+)
-- Matches the "Guild Bot Data Model" doc: 17 tables in 5 areas.
-- Run inside a fresh database:  psql -d guildbot -f guild_bot_oltp.sql
-- =====================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS guild;
SET search_path = guild, public;

-- ---------------------------------------------------------------------
-- Enum types
-- ---------------------------------------------------------------------
CREATE TYPE wow_region    AS ENUM ('us', 'eu', 'kr', 'tw', 'cn');
CREATE TYPE wow_role      AS ENUM ('tank', 'healer', 'dps');
CREATE TYPE activity_type AS ENUM ('raid', 'mplus', 'leveling', 'delves', 'pvp');
CREATE TYPE stats_source  AS ENUM ('raiderio', 'blizzard');
CREATE TYPE event_type    AS ENUM ('raid', 'mplus', 'leveling', 'delves', 'pvp', 'other');
CREATE TYPE event_status  AS ENUM ('open', 'full', 'done', 'cancelled');
CREATE TYPE signup_status AS ENUM ('signed', 'bench', 'attended', 'no_show');
CREATE TYPE raid_difficulty AS ENUM ('lfr', 'normal', 'heroic', 'mythic');

-- ---------------------------------------------------------------------
-- Reference data (seeded from Blizzard static game data, per season)
-- ---------------------------------------------------------------------
CREATE TABLE class (
    class_id    smallint PRIMARY KEY,           -- Blizzard playable-class ID
    name        text NOT NULL UNIQUE
);

CREATE TABLE spec (
    spec_id     smallint PRIMARY KEY,           -- Blizzard playable-spec ID
    class_id    smallint NOT NULL REFERENCES class (class_id),
    name        text NOT NULL,
    role        wow_role NOT NULL,
    UNIQUE (class_id, name)
);

CREATE TABLE season (
    season_id   smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug        text NOT NULL UNIQUE,           -- e.g. Raider.IO season slug
    expansion   text NOT NULL,
    name        text NOT NULL,
    starts_at   timestamptz,
    ends_at     timestamptz,
    CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at)
);

CREATE TABLE dungeon (
    dungeon_id  integer PRIMARY KEY,            -- Blizzard / Raider.IO zone ID
    name        text NOT NULL,
    short_name  text,
    season_id   smallint REFERENCES season (season_id)   -- current M+ rotation
);

CREATE TABLE raid (
    raid_id     integer PRIMARY KEY,
    name        text NOT NULL,
    boss_count  smallint NOT NULL CHECK (boss_count > 0),
    season_id   smallint REFERENCES season (season_id)
);

-- ---------------------------------------------------------------------
-- Identity and consent  (the ONLY place PII lives)
-- ---------------------------------------------------------------------
CREATE TABLE player (
    player_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    discord_id       bigint NOT NULL UNIQUE,     -- Discord snowflake
    bnet_account_id  bigint UNIQUE,              -- set after Battle.net OAuth
    battletag        text,
    timezone         text NOT NULL DEFAULT 'America/Chicago',  -- IANA name
    consent_version  text NOT NULL,
    consented_at     timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE oauth_link_state (
    state       text PRIMARY KEY,                -- random, single use
    discord_id  bigint NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL DEFAULT now() + interval '10 minutes'
);
CREATE INDEX oauth_link_state_expires_idx ON oauth_link_state (expires_at);

CREATE TABLE deletion_log (
    discord_id_hash  bytea PRIMARY KEY,          -- HMAC-SHA256(discord_id, secret salt)
    deleted_at       timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Characters and preferences
-- ---------------------------------------------------------------------
CREATE TABLE "character" (
    character_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id             uuid NOT NULL REFERENCES player (player_id) ON DELETE CASCADE,
    blizzard_character_id bigint UNIQUE,
    region                wow_region NOT NULL,
    realm_slug            text NOT NULL,
    name                  text NOT NULL,
    class_id              smallint REFERENCES class (class_id),
    is_main               boolean NOT NULL DEFAULT false,
    verified              boolean NOT NULL DEFAULT false,  -- came from Battle.net OAuth
    linked_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (region, realm_slug, name)
);
CREATE INDEX character_player_idx ON "character" (player_id);
-- At most one main per player
CREATE UNIQUE INDEX character_one_main_idx ON "character" (player_id) WHERE is_main;

CREATE TABLE character_role (
    character_id     bigint NOT NULL REFERENCES "character" (character_id) ON DELETE CASCADE,
    role             wow_role NOT NULL,
    preference_rank  smallint NOT NULL DEFAULT 1 CHECK (preference_rank BETWEEN 1 AND 3),
    PRIMARY KEY (character_id, role)
);
CREATE INDEX character_role_role_idx ON character_role (role);

CREATE TABLE player_interest (
    player_id  uuid NOT NULL REFERENCES player (player_id) ON DELETE CASCADE,
    activity   activity_type NOT NULL,
    goal       text CHECK (length(goal) <= 200),   -- e.g. 'push 12s'
    PRIMARY KEY (player_id, activity)
);

CREATE TABLE availability_window (
    window_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id    uuid NOT NULL REFERENCES player (player_id) ON DELETE CASCADE,
    day_of_week  smallint NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),  -- 0 = Sunday
    start_local  time NOT NULL,      -- in player.timezone
    end_local    time NOT NULL,      -- may be < start_local for windows past midnight
    activity     activity_type,      -- NULL = any activity
    CHECK (start_local <> end_local)
);
CREATE INDEX availability_window_player_idx ON availability_window (player_id);
CREATE INDEX availability_window_day_idx    ON availability_window (day_of_week);

-- ---------------------------------------------------------------------
-- Cached stats (Raider.IO / Blizzard are the source of truth)
-- ---------------------------------------------------------------------
CREATE TABLE character_stats (
    character_id   bigint PRIMARY KEY REFERENCES "character" (character_id) ON DELETE CASCADE,
    level          smallint CHECK (level BETWEEN 1 AND 100),
    spec_id        smallint REFERENCES spec (spec_id),
    item_level     numeric(5,1),
    mplus_score    numeric(7,1),
    raid_progress  jsonb NOT NULL DEFAULT '{}'::jsonb,
    source         stats_source NOT NULL,
    fetched_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX character_stats_score_idx ON character_stats (mplus_score DESC);

CREATE TABLE mplus_run (
    run_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    raiderio_run_id  bigint NOT NULL,
    character_id     bigint NOT NULL REFERENCES "character" (character_id) ON DELETE CASCADE,
    dungeon_id       integer NOT NULL REFERENCES dungeon (dungeon_id),
    season_id        smallint NOT NULL REFERENCES season (season_id),
    key_level        smallint NOT NULL CHECK (key_level >= 2),
    upgrades         smallint NOT NULL DEFAULT 0 CHECK (upgrades BETWEEN 0 AND 3),  -- 0 = not timed
    score            numeric(6,1),
    completed_at     timestamptz NOT NULL,
    UNIQUE (raiderio_run_id, character_id)
);
CREATE INDEX mplus_run_character_idx ON mplus_run (character_id, season_id);
CREATE INDEX mplus_run_dungeon_idx   ON mplus_run (dungeon_id, key_level DESC);

-- ---------------------------------------------------------------------
-- Groups and events
-- ---------------------------------------------------------------------
CREATE TABLE event (
    event_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    type                 event_type NOT NULL,
    title                text NOT NULL CHECK (length(title) <= 100),
    organizer_player_id  uuid REFERENCES player (player_id) ON DELETE SET NULL,
    scheduled_start      timestamptz NOT NULL,
    dungeon_id           integer REFERENCES dungeon (dungeon_id),
    raid_id              integer REFERENCES raid (raid_id),
    difficulty           raid_difficulty,
    key_level            smallint CHECK (key_level >= 2),
    status               event_status NOT NULL DEFAULT 'open',
    created_at           timestamptz NOT NULL DEFAULT now(),
    CHECK (dungeon_id IS NULL OR raid_id IS NULL)
);
CREATE INDEX event_start_idx ON event (scheduled_start) WHERE status IN ('open', 'full');

CREATE TABLE event_signup (
    event_id      bigint NOT NULL REFERENCES event (event_id) ON DELETE CASCADE,
    character_id  bigint NOT NULL REFERENCES "character" (character_id) ON DELETE CASCADE,
    role          wow_role NOT NULL,
    status        signup_status NOT NULL DEFAULT 'signed',
    signed_up_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, character_id)
);
CREATE INDEX event_signup_character_idx ON event_signup (character_id);

-- ---------------------------------------------------------------------
-- Operations
-- ---------------------------------------------------------------------
CREATE TABLE agent_query_log (
    query_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id   uuid REFERENCES player (player_id) ON DELETE SET NULL,
    tool_name   text NOT NULL,
    success     boolean NOT NULL,
    latency_ms  integer CHECK (latency_ms >= 0),
    tokens      integer CHECK (tokens >= 0),
    created_at  timestamptz NOT NULL DEFAULT now()
    -- Deliberately no message text.
);
CREATE INDEX agent_query_log_created_idx ON agent_query_log (created_at);

-- ---------------------------------------------------------------------
-- Housekeeping
-- ---------------------------------------------------------------------
CREATE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

CREATE TRIGGER player_touch_updated_at
    BEFORE UPDATE ON player
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------------------------------------------------------------------
-- Agent-facing view: no Discord, Battle.net or BattleTag columns
-- ---------------------------------------------------------------------
CREATE VIEW player_public AS
    SELECT player_id, timezone
    FROM player;

-- ---------------------------------------------------------------------
-- Roles
--   guild_agent_ro : the LLM agent's tools (read-only, no PII)
--   guild_bot_rw   : slash commands, OAuth callback, nightly refresh
-- Create login users separately and GRANT these roles to them.
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'guild_agent_ro') THEN
        CREATE ROLE guild_agent_ro NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'guild_bot_rw') THEN
        CREATE ROLE guild_bot_rw NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA guild TO guild_agent_ro, guild_bot_rw;

-- Agent: SELECT on everything except the PII and ops tables
GRANT SELECT ON
    class, spec, season, dungeon, raid,
    "character", character_role, player_interest, availability_window,
    character_stats, mplus_run, event, event_signup,
    player_public
TO guild_agent_ro;

-- Bot: full DML, plus sequences for identity columns
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA guild TO guild_bot_rw;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA guild TO guild_bot_rw;

COMMIT;