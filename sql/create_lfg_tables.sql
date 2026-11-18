-- Creates guild.lfg_listing and guild.lfg_signup (looking for group).
-- Run as a role allowed to create tables in guild. There are deliberately no foreign keys to
-- guild.player or guild."character"; data_tools.delete_player removes a player's LFG rows instead.
-- Safe to run more than once.
--
--   psql "$DATABASE_URL" -f sql/create_lfg_tables.sql

-- Looking-for-group listings (dungeons and leveling). 'roles' listings cap each role;
-- 'open' listings only cap the total party size.
CREATE TABLE IF NOT EXISTS guild.lfg_listing (
    listing_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    host_id     text NOT NULL,      -- guild.player.player_id as text (no FK; the type varies)
    guild_id    bigint NOT NULL,
    channel_id  bigint,
    message_id  bigint,
    kind        text NOT NULL CHECK (kind IN ('dungeon', 'leveling')),
    activity    text NOT NULL,
    notes       text,
    mode        text NOT NULL CHECK (mode IN ('roles', 'open')),
    tanks       integer NOT NULL DEFAULT 0,
    healers     integer NOT NULL DEFAULT 0,
    dps         integer NOT NULL DEFAULT 0,
    size        integer NOT NULL,
    starts_at   timestamptz,
    status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE guild.lfg_listing
    ADD COLUMN IF NOT EXISTS starts_at timestamptz;

CREATE TABLE IF NOT EXISTS guild.lfg_signup (
    listing_id   bigint NOT NULL REFERENCES guild.lfg_listing ON DELETE CASCADE,
    player_id    text NOT NULL,     -- guild.player.player_id as text
    character_id text NOT NULL,     -- guild."character".character_id as text
    role         text NOT NULL CHECK (role IN ('tank', 'healer', 'dps')),
    signed_up_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (listing_id, player_id)
);

CREATE INDEX IF NOT EXISTS lfg_listing_open ON guild.lfg_listing (guild_id) WHERE status = 'open';

GRANT SELECT, INSERT, UPDATE, DELETE ON guild.lfg_listing, guild.lfg_signup TO ro_usr_captain_cookie;
