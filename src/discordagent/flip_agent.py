"""/flip agent: Claude queries the auction_rpt star schema to find flip opportunities."""

import json
import logging

import anthropic
import asyncpg

log = logging.getLogger(__name__)

MAX_ITERATIONS = 15
MAX_ROWS = 100
STATEMENT_TIMEOUT_MS = 15_000

FLIP_SYSTEM_PROMPT = """You are Captain Cookie, a World of Warcraft (Horde) auction house analyst. \
Find items worth flipping: items currently listed far below (or historically selling far from) their typical price. \
You answer by querying a Postgres reporting schema with the `query_auction_db` tool (read-only SELECT).

## Data model (schema `auction_rpt`, always schema-qualify tables)
All prices are in COPPER per unit (10000 copper = 1 gold, 100 copper = 1 silver).
Aggregated to ITEM granularity from a snapshot of the Horde auction house taken every hour.

auction_rpt.dim_item — SCD2 item dimension (one row per item version)
- item_key bigint (surrogate key, PK), item_id bigint (Blizzard natural key)
- item_name varchar(255), quality varchar (e.g. Poor/Common/Uncommon/Rare/Epic), item_class, item_subclass
- vendor_price bigint (copper, what a vendor pays; nullable)
- is_active boolean (exactly one active row per item_id), created_at, updated_at
- start_timestamp, end_timestamp (version validity; open-ended end = 9999-12-31 23:59:59)

auction_rpt.item_fact — SCD2 price fact (one row per version of an item's market numbers)
- item_fact_id bigint PK, item_key bigint -> dim_item.item_key, item_id bigint
- min_price bigint NOT NULL: cheapest current listing per unit
- avg_price bigint (nullable): quantity-weighted mean listing price. UNRELIABLE: a few outlier listings at absurd prices inflate it far above min_price, so NEVER use avg_price alone to decide something is cheap.
- listings int (nullable): number of active listings
- is_active boolean: TRUE = the current snapshot; unique per item_id. Items no longer listed have no active row.
- created_at, updated_at, start_timestamp, end_timestamp: a new row is written only when an item's numbers change; the previous row is closed (is_active=false, end_timestamp set). Row history is therefore a time series of the item's market.

Join: item_fact.item_key = dim_item.item_key (use the dim_item row whose is_active is true, or the matching version).

## How to find flips
1. Current market: item_fact WHERE is_active.
2. Build a ROBUST baseline from history instead of avg_price: e.g. percentile_cont(0.5) WITHIN GROUP (ORDER BY min_price) over the item's fact rows from the last 7-14 days (start_timestamp > now() - interval '14 days'), excluding the current row. Compare the current min_price against it (ratio = baseline / current min_price). Require several history rows (count >= 5) so the baseline is meaningful.
3. Use avg_price only as a secondary, noisy signal; a large avg_price/min_price gap alone usually means outliers, not opportunity.
4. Sanity filters: min_price > 0, listings >= 2 (thin markets are traps), vendor_price when present (min_price below vendor_price is a vendor flip), and exclude junk (e.g. quality 'Poor') unless asked.
5. Rank by estimated profit per unit (baseline - min_price - 5% AH cut on baseline), then ratio. Return the top 10-15 unless the user asks otherwise.

## Rules
- Only SELECT/WITH queries. Always include LIMIT (max 100 rows are returned). Explore with a small query first if unsure (e.g. check a few rows or the history depth).
- If a query errors, read the message, fix it, and retry.
- Final answer: a compact Discord-friendly list/table. For each item: name, current min price, baseline, estimated profit, listings. Format money as gold/silver/copper (e.g. 12g 50s). Briefly note caveats (thin history, outliers). Keep it under ~1800 characters.
- If the user gives a focus (item class, budget, quality), apply it."""

FLIP_TOOL = {
    "name": "query_auction_db",
    "description": (
        "Run one read-only SQL SELECT (or WITH ... SELECT) against the auction_rpt "
        f"schema. Returns up to {MAX_ROWS} rows as JSON. Prices are copper per unit."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A single SELECT statement."},
        },
        "required": ["sql"],
    },
}


async def run_query(pool: asyncpg.Pool, sql: str) -> str:
    """Execute model-written SQL in a read-only transaction; return JSON for the model."""
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                await conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
                rows = await conn.fetch(sql)
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        return json.dumps({"error": str(e)})
    truncated = len(rows) > MAX_ROWS
    return json.dumps(
        {
            "row_count": len(rows),
            "truncated": truncated,
            "rows": [dict(r) for r in rows[:MAX_ROWS]],
        },
        default=str,
    )


async def run_flip(pool: asyncpg.Pool, request: str | None) -> str:
    """Run the flip-finding agent loop and return its final text answer."""
    client = anthropic.AsyncAnthropic()
    prompt = request or "Find the best items to flip right now."
    messages = [{"role": "user", "content": prompt}]

    for iteration in range(MAX_ITERATIONS):
        response = await client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=[
                {
                    "type": "text",
                    "text": FLIP_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[FLIP_TOOL],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return (
                "\n".join(b.text for b in response.content if b.type == "text").strip()
                or "No flips found."
            )

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            log.info("/flip query (iter %d): %s", iteration, block.input.get("sql"))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": await run_query(pool, block.input.get("sql", "")),
                }
            )
        messages.append({"role": "user", "content": results})

    return "Ran out of steps before finishing the analysis; try a narrower request."
