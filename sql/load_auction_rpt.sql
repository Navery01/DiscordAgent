CREATE OR REPLACE PROCEDURE auction_rpt.load_item_scd2()
LANGUAGE plpgsql
AS $BODY$
DECLARE
    v_now   timestamptz := now();
    v_open  constant timestamptz := '9999-12-31 23:59:59';
    v_rows  integer;
BEGIN
    -- Serialize concurrent runs; two loads interleaving would both try to open new versions.
    PERFORM pg_advisory_xact_lock(hashtext('auction_rpt.load_item_scd2'));

    ------------------------------------------------------------------------------------
    -- 1. dim_item
    ------------------------------------------------------------------------------------
    -- Source: every looked-up item, plus an "inferred member" placeholder for auctioned
    -- items the item lookup hasn't fetched yet, so every fact row can get an item_key.
    -- When the real lookup lands, the attributes differ and step 1a versions it.
    DROP TABLE IF EXISTS _src_item;
    CREATE TEMP TABLE _src_item ON COMMIT DROP AS
    SELECT i.item_id,
           coalesce(i.name, 'Unknown item ' || i.item_id) AS item_name,
           i.quality,
           i.item_class,
           i.item_subclass,
           i.sell_price AS vendor_price
      FROM guild.item i
    UNION ALL
    SELECT DISTINCT a.item_id,
           'Unknown item ' || a.item_id,
           NULL, NULL, NULL,
           NULL::bigint
      FROM guild.auction a
     WHERE NOT EXISTS (SELECT 1 FROM guild.item i WHERE i.item_id = a.item_id);

    -- 1a. Expire the active version of any item whose tracked attributes changed.
    UPDATE auction_rpt.dim_item d
       SET is_active     = false,
           end_timestamp = v_now,
           updated_at    = v_now
      FROM _src_item s
     WHERE d.item_id = s.item_id
       AND d.is_active
       AND (d.item_name, d.quality, d.item_class, d.item_subclass, d.vendor_price)
           IS DISTINCT FROM
           (s.item_name, s.quality, s.item_class, s.item_subclass, s.vendor_price);
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RAISE NOTICE 'dim_item: % versions expired', v_rows;

    -- 1b. Open a new version for new items and for the ones just expired.
    INSERT INTO auction_rpt.dim_item
           (item_id, item_name, quality, item_class, item_subclass, vendor_price,
            is_active, created_at, updated_at, start_timestamp, end_timestamp)
    SELECT s.item_id, s.item_name, s.quality, s.item_class, s.item_subclass, s.vendor_price,
           true, v_now, v_now, v_now, v_open
      FROM _src_item s
     WHERE NOT EXISTS (SELECT 1 FROM auction_rpt.dim_item d
                        WHERE d.item_id = s.item_id AND d.is_active);
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RAISE NOTICE 'dim_item: % versions opened', v_rows;

    ------------------------------------------------------------------------------------
    -- 2. item_fact
    ------------------------------------------------------------------------------------
    -- An empty snapshot almost certainly means a failed poll, not an empty auction house;
    -- loading it would expire every active fact row.
    IF NOT EXISTS (SELECT 1 FROM guild.auction) THEN
        RAISE WARNING 'guild.auction is empty; skipping item_fact load';
        RETURN;
    END IF;

    -- Current market numbers per item, tagged with the item's active dimension version.
    DROP TABLE IF EXISTS _src_fact;
    CREATE TEMP TABLE _src_fact ON COMMIT DROP AS
    SELECT a.item_id,
           d.item_key,
           min(a.unit_price) AS min_price,
           round(sum(a.unit_price::numeric * a.quantity)
                 / nullif(sum(a.quantity), 0))::bigint AS avg_price,
           count(*)::int AS listings
      FROM guild.auction a
      JOIN auction_rpt.dim_item d ON d.item_id = a.item_id AND d.is_active
     WHERE a.unit_price IS NOT NULL
     GROUP BY a.item_id, d.item_key;

    -- 2a. Expire active versions whose numbers changed, whose item is no longer listed,
    --     or whose dimension version was superseded in step 1.
    UPDATE auction_rpt.item_fact f
       SET is_active     = false,
           end_timestamp = v_now,
           updated_at    = v_now
     WHERE f.is_active
       AND NOT EXISTS (
             SELECT 1 FROM _src_fact s
              WHERE s.item_id   = f.item_id
                AND s.item_key  = f.item_key
                AND s.min_price = f.min_price
                AND s.avg_price IS NOT DISTINCT FROM f.avg_price
                AND s.listings  IS NOT DISTINCT FROM f.listings);
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RAISE NOTICE 'item_fact: % versions expired', v_rows;

    -- 2b. Open a new version for every listed item without an active one.
    INSERT INTO auction_rpt.item_fact
           (item_key, item_id, min_price, avg_price, listings,
            is_active, created_at, updated_at, start_timestamp, end_timestamp)
    SELECT s.item_key, s.item_id, s.min_price, s.avg_price, s.listings,
           true, v_now, v_now, v_now, v_open
      FROM _src_fact s
     WHERE NOT EXISTS (SELECT 1 FROM auction_rpt.item_fact f
                        WHERE f.item_id = s.item_id AND f.is_active);
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RAISE NOTICE 'item_fact: % versions opened', v_rows;
END;
$BODY$;
ALTER PROCEDURE auction_rpt.load_item_scd2()
    OWNER TO ro_usr_captain_cookie;
