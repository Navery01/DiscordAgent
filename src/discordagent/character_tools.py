"""Agent tool: look up a Discord user's main WoW character (gear, stats, professions)."""

import asyncio
import logging

import aiohttp

from . import data_tools

log = logging.getLogger(__name__)

CHARACTER_TOOL = {
    "name": "get_main_character_details",
    "description": (
        "Inspect the requesting Discord user's main World of Warcraft character "
        "(or their first linked character if no main is set). Returns live data "
        "from Blizzard: level, class, equipped gear, combat stats and professions. "
        "Use `sections` to fetch only what is needed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "items": {"type": "string", "enum": ["gear", "stats", "professions"]},
                "description": "Which sections to include. Defaults to all.",
            },
        },
        "required": [],
    },
}


def _rating(stat: dict | None) -> dict | None:
    if not stat:
        return None
    return {"rating": stat.get("rating"), "percent": round(stat.get("value", 0), 2)}


def _format_gear(equipment: dict) -> list[dict]:
    items = []
    for item in equipment.get("equipped_items") or []:
        entry = {
            "slot": (item.get("slot") or {}).get("type"),
            "name": item.get("name"),
            "item_level": (item.get("level") or {}).get("value"),
            "quality": (item.get("quality") or {}).get("type"),
        }
        enchants = [e.get("display_string") for e in item.get("enchantments") or []]
        if enchants:
            entry["enchants"] = enchants
        gems = [
            s["display_string"]
            for s in item.get("sockets") or []
            if s.get("display_string")
        ]
        if gems:
            entry["gems"] = gems
        items.append(entry)
    return items


def _format_stats(stats: dict) -> dict:
    def effective(key: str):
        return (stats.get(key) or {}).get("effective")

    return {
        "health": stats.get("health"),
        "power_type": (stats.get("power_type") or {}).get("name"),
        "power": stats.get("power"),
        "strength": effective("strength"),
        "agility": effective("agility"),
        "intellect": effective("intellect"),
        "stamina": effective("stamina"),
        "armor": effective("armor"),
        "crit": _rating(stats.get("melee_crit") or stats.get("spell_crit")),
        "haste": _rating(stats.get("melee_haste") or stats.get("spell_haste")),
        "mastery": _rating(stats.get("mastery")),
        "versatility_rating": stats.get("versatility"),
        "versatility_damage_bonus_percent": stats.get("versatility_damage_done_bonus"),
    }


def _format_professions(professions: dict) -> dict:
    def collect(entries: list[dict] | None) -> list[dict]:
        return [
            {
                "name": (e.get("profession") or {}).get("name"),
                "skill_points": e.get("skill_points"),
                "max_skill_points": e.get("max_skill_points"),
                "tiers": [
                    {
                        "name": (t.get("tier") or {}).get("name"),
                        "skill_points": t.get("skill_points"),
                        "max_skill_points": t.get("max_skill_points"),
                    }
                    for t in e.get("tiers") or []
                ],
            }
            for e in entries or []
        ]

    return {
        "primary": collect(professions.get("primaries")),
        "secondary": collect(professions.get("secondaries")),
    }


async def _fetch(label: str, coro, formatter) -> dict:
    """Run one Blizzard call; a failure becomes an error entry instead of failing the tool."""
    try:
        return {label: formatter(await coro)}
    except aiohttp.ClientResponseError as e:
        if e.status == 404:
            return {label: {"error": "not available in the Blizzard API (character not public?)"}}
        log.warning("Blizzard %s fetch failed: HTTP %s", label, e.status)
    except (aiohttp.ClientError, TimeoutError):
        log.warning("Blizzard %s fetch failed", label, exc_info=True)
    return {label: {"error": "Blizzard API request failed"}}


async def get_main_character_details(
    db, blizzard, discord_id: int | None, sections: list[str] | None = None
) -> dict:
    if db is None or blizzard is None:
        return {"success": False, "error": "Character lookup isn't configured (database or Blizzard credentials missing)."}
    if discord_id is None:
        return {"success": False, "error": "No requesting user is known for this task."}

    linked = await data_tools.get_player_main_or_first_character(db, discord_id)
    if linked is None:
        return {"success": False, "error": "The user has no linked character. They need to run /link first."}

    wanted = set(sections or ["gear", "stats", "professions"])
    region, realm, name = linked["region"], linked["realm_slug"], linked["name"]

    calls = [_fetch("summary", blizzard.get_character_summary(region, realm, name), _format_summary)]
    if "gear" in wanted:
        calls.append(_fetch("gear", blizzard.get_character_equipment(region, name), _format_gear))
    if "stats" in wanted:
        calls.append(_fetch("stats", blizzard.get_character_statistics(region, name), _format_stats))
    if "professions" in wanted:
        calls.append(
            _fetch("professions", blizzard.get_character_professions(region, name), _format_professions)
        )

    result = {"success": True, "name": name, "realm": realm, "region": region}
    for part in await asyncio.gather(*calls):
        result.update(part)
    return result


def _format_summary(summary: dict) -> dict:
    return {
        "level": summary.get("level"),
        "class": (summary.get("character_class") or {}).get("name"),
        "spec": (summary.get("active_spec") or {}).get("name"),
        "race": (summary.get("race") or {}).get("name"),
        "faction": (summary.get("faction") or {}).get("name"),
        "guild": (summary.get("guild") or {}).get("name"),
        "equipped_item_level": summary.get("equipped_item_level"),
        "average_item_level": summary.get("average_item_level"),
    }
