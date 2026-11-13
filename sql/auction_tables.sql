CREATE SCHEMA IF NOT EXISTS auction_rpt;

CREATE TABLE IF NOT EXISTS guild.auction
(
    auction_id bigint NOT NULL,
    item_id bigint NOT NULL,
    quantity integer NOT NULL,
    unit_price bigint,
    time_left text COLLATE pg_catalog."default" NOT NULL,
    CONSTRAINT auction_pkey PRIMARY KEY (auction_id)
)

TABLESPACE pg_default;

ALTER TABLE guild.auction
    OWNER to ro_usr_captain_cookie;

-- Index: guild.auction_item_id
CREATE INDEX IF NOT EXISTS auction_item_id
    ON guild.auction USING btree
    (item_id ASC NULLS LAST)
    TABLESPACE pg_default;

CREATE TABLE IF NOT EXISTS guild.item
(
    item_id bigint NOT NULL,
    name text COLLATE pg_catalog."default",
    quality text COLLATE pg_catalog."default",
    item_class text COLLATE pg_catalog."default",
    item_subclass text COLLATE pg_catalog."default",
    "raw" jsonb NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    sell_price bigint,
    purchase_price bigint,
    CONSTRAINT item_pkey PRIMARY KEY (item_id)
)

TABLESPACE pg_default;

ALTER TABLE guild.item
    OWNER to ro_usr_captain_cookie;


-- SCD2 item dimension: one row per version of an item. item_key is the surrogate key,
-- item_id the Blizzard natural key. Exactly one is_active row per item_id.
CREATE TABLE IF NOT EXISTS auction_rpt.dim_item (
    item_key bigserial PRIMARY KEY,
    item_id bigint NOT NULL,
    item_name VARCHAR(255) NOT NULL,
    quality VARCHAR(255),
    item_class VARCHAR(255),
    item_subclass VARCHAR(255),
    vendor_price bigint,                    -- copper; guild.item.sell_price
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    start_timestamp timestamptz NOT NULL DEFAULT now(),
    end_timestamp timestamptz NOT NULL DEFAULT '9999-12-31 23:59:59'
);
CREATE UNIQUE INDEX IF NOT EXISTS dim_item_active_item_id
    ON auction_rpt.dim_item (item_id) WHERE is_active;

-- SCD2 price fact: a new version whenever an item's market numbers change; the active
-- version is closed when the item is no longer listed. Prices are copper per unit.
CREATE TABLE IF NOT EXISTS auction_rpt.item_fact (
    item_fact_id bigserial PRIMARY KEY,
    item_key bigint NOT NULL REFERENCES auction_rpt.dim_item (item_key),
    item_id bigint NOT NULL,
    min_price bigint NOT NULL,
    avg_price bigint,                       -- quantity-weighted
    listings INT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    start_timestamp timestamptz NOT NULL DEFAULT now(),
    end_timestamp timestamptz NOT NULL DEFAULT '9999-12-31 23:59:59'
);
CREATE UNIQUE INDEX IF NOT EXISTS item_fact_active_item_id
    ON auction_rpt.item_fact (item_id) WHERE is_active;
CREATE INDEX IF NOT EXISTS item_fact_item_key ON auction_rpt.item_fact (item_key);

-- Table owners need schema USAGE too: FK checks run as the owner of the referenced table.
GRANT USAGE ON SCHEMA auction_rpt TO ro_usr_captain_cookie;
ALTER TABLE auction_rpt.dim_item OWNER TO ro_usr_captain_cookie;
ALTER TABLE auction_rpt.item_fact OWNER TO ro_usr_captain_cookie;