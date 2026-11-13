-- Creates guild.auction (Horde commodity auctions) and guild.item (item lookup).
-- Safe to run more than once. Keep in sync with SCHEMA in src/discordagent/data_tools.py.
--
--   psql "$DATABASE_URL" -f sql/create_auction_tables.sql

CREATE TABLE IF NOT EXISTS guild.auction (
    auction_id bigint PRIMARY KEY,
    item_id    bigint NOT NULL,
    quantity   integer NOT NULL,
    unit_price bigint,              -- copper per unit
    time_left  text NOT NULL        -- SHORT, MEDIUM, LONG, VERY_LONG
);
CREATE INDEX IF NOT EXISTS auction_item_id ON guild.auction (item_id);

CREATE TABLE IF NOT EXISTS guild.item (
    item_id       bigint PRIMARY KEY,
    name          text,             -- NULL if Blizzard has no data for this item
    quality       text,             -- e.g. COMMON, RARE, EPIC
    item_class    text,
    item_subclass text,
    raw           jsonb NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- The bot connects as its own role, which gets no access to tables other roles create.
GRANT SELECT, INSERT, UPDATE, DELETE ON guild.auction, guild.item TO ro_usr_captain_cookie;

  ALTER TABLE guild.auction OWNER TO ro_usr_captain_cookie;
  ALTER TABLE guild.item OWNER TO ro_usr_captain_cookie;