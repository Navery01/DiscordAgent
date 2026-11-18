"""Start-time and timezone parsing for LFG listings.

Discord has no native date/time picker for slash commands, so /lfg create offers
autocomplete suggestions built from these helpers and accepts loose typed input
such as "8pm", "tomorrow 7:30pm", "fri 9", "in 45m" or "2026-11-20 20:30".
"""

import re
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

# A start time this far in the past still counts as "today" (the group may have
# just started); anything older rolls forward to the next occurrence.
GRACE = timedelta(hours=1)

# (label, IANA zone) shown first in the timezone autocomplete.
COMMON_TIMEZONES = [
    ("US Eastern", "America/New_York"),
    ("US Central", "America/Chicago"),
    ("US Mountain", "America/Denver"),
    ("US Arizona", "America/Phoenix"),
    ("US Pacific", "America/Los_Angeles"),
    ("US Alaska", "America/Anchorage"),
    ("US Hawaii", "Pacific/Honolulu"),
    ("UTC", "UTC"),
    ("UK / Ireland", "Europe/London"),
    ("Central Europe", "Europe/Berlin"),
    ("Eastern Europe", "Europe/Helsinki"),
    ("Moscow", "Europe/Moscow"),
    ("Brazil (São Paulo)", "America/Sao_Paulo"),
    ("Argentina", "America/Argentina/Buenos_Aires"),
    ("Mexico City", "America/Mexico_City"),
    ("Canada Atlantic", "America/Halifax"),
    ("Australia Eastern", "Australia/Sydney"),
    ("Australia Central", "Australia/Adelaide"),
    ("Australia Western", "Australia/Perth"),
    ("New Zealand", "Pacific/Auckland"),
    ("Japan / Korea", "Asia/Tokyo"),
    ("China / Singapore", "Asia/Singapore"),
    ("India", "Asia/Kolkata"),
]

# Common abbreviations. DST variants map to the same zone so the right offset is
# used for whatever date the group is on.
ABBREVIATIONS = {
    "utc": "UTC",
    "gmt": "UTC",
    "z": "UTC",
    "et": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
    "ct": "America/Chicago",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "mt": "America/Denver",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "pt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "akst": "America/Anchorage",
    "akdt": "America/Anchorage",
    "hst": "Pacific/Honolulu",
    "ast": "America/Halifax",
    "adt": "America/Halifax",
    "bst": "Europe/London",
    "wet": "Europe/Lisbon",
    "west": "Europe/Lisbon",
    "cet": "Europe/Berlin",
    "cest": "Europe/Berlin",
    "eet": "Europe/Helsinki",
    "eest": "Europe/Helsinki",
    "msk": "Europe/Moscow",
    "brt": "America/Sao_Paulo",
    "ist": "Asia/Kolkata",
    "jst": "Asia/Tokyo",
    "kst": "Asia/Seoul",
    "sgt": "Asia/Singapore",
    "aest": "Australia/Sydney",
    "aedt": "Australia/Sydney",
    "acst": "Australia/Adelaide",
    "acdt": "Australia/Adelaide",
    "awst": "Australia/Perth",
    "nzst": "Pacific/Auckland",
    "nzdt": "Pacific/Auckland",
}

_OFFSET_RE = re.compile(r"^(?:utc|gmt)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$")
_REL_RE = re.compile(
    r"^(?:in\s+)?(?:(\d+)\s*(?:h|hr|hrs|hour|hours))?\s*(?:(\d+)\s*(?:m|min|mins|minute|minutes))?$"
)
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}[ tT]\d{1,2}:\d{2}")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_US_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?\b")
_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?$")
_WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "weds": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}
_TOMORROW = {"tomorrow", "tmrw", "tmr", "tom"}
_TODAY = {"today", "tonight", "tn"}


@lru_cache(maxsize=1)
def all_zone_names() -> tuple[str, ...]:
    return tuple(sorted(available_timezones()))


@lru_cache(maxsize=1)
def _zones_by_lower() -> dict[str, str]:
    return {z.lower(): z for z in all_zone_names()}


def resolve_timezone(text: str | None) -> tzinfo | None:
    """Turn 'America/New_York', 'EST', 'UTC-5' or '+05:30' into a tzinfo; None if unknown."""
    if not text or not text.strip():
        return None
    key = text.strip().lower()
    if key in ABBREVIATIONS:
        return ZoneInfo(ABBREVIATIONS[key])
    m = _OFFSET_RE.match(key)
    if m:
        sign, hours, minutes = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        if hours > 14 or minutes >= 60:
            return None
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(-delta if sign == "-" else delta)
    if key in ("utc", "gmt"):
        return UTC
    name = _zones_by_lower().get(key.replace(" ", "_"))
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return None
    return None


def parse_start_time(
    text: str, tz: tzinfo, now: datetime | None = None
) -> datetime | None:
    """Parse a loosely written start time in `tz`. Returns an aware UTC datetime, or None.

    Accepts: 'now', 'in 30m' / 'in 1h30m' / '2 hours', '8pm', '20:30', 'tonight 8',
    'tomorrow 7:30pm', 'fri 9pm', '11/20 8pm', '2026-11-20 20:30', and ISO-8601
    (an explicit offset in the text wins over `tz`). A time with no day means its
    next occurrence; a bare hour like '8' picks whichever of 8 AM / 8 PM comes next.
    """
    raw = " ".join(text.strip().split())
    # A trailing zone in the text ("8pm est", "20:00 UTC+2") overrides `tz`.
    head, _, last = raw.rpartition(" ")
    if head and (last.lower() in ABBREVIATIONS or _OFFSET_RE.match(last.lower())):
        override = resolve_timezone(last)
        if override is not None:
            tz, raw = override, head
    now = (now or datetime.now(UTC)).astimezone(tz)
    s = raw.lower()
    if not s:
        return None
    if s in ("now", "asap", "right now"):
        return now.astimezone(UTC).replace(second=0, microsecond=0)

    m = _REL_RE.match(s)
    if m and (m.group(1) or m.group(2)):
        delta = timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
        return (now + delta).astimezone(UTC).replace(second=0, microsecond=0)

    dt = None
    if _ISO_DATETIME_RE.match(raw):
        try:
            dt = datetime.fromisoformat(raw.upper())
        except ValueError:
            pass
    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(UTC)

    return _parse_day_and_time(s, tz, now)


def _parse_day_and_time(s: str, tz: tzinfo, now: datetime) -> datetime | None:
    s = s.replace(",", " ").replace("@", " ")
    today = now.date()
    day: date | None = None
    tonight = False

    m = _ISO_DATE_RE.search(s)
    if m:
        day = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if day is None:
            return None
        s = s[: m.start()] + s[m.end() :]
    else:
        m = _US_DATE_RE.search(s)
        if m:
            year = m.group(3)
            yr = (
                (int(year) + 2000 if len(year) == 2 else int(year))
                if year
                else today.year
            )
            day = _safe_date(yr, int(m.group(1)), int(m.group(2)))
            if day is None:
                return None
            if not year and day < today - timedelta(days=1):
                day = _safe_date(yr + 1, day.month, day.day)
            s = s[: m.start()] + s[m.end() :]

    words = [w for w in s.split() if w != "at"]
    rest = []
    for w in words:
        if w in _TOMORROW and day is None:
            day = today + timedelta(days=1)
        elif w in _TODAY and day is None:
            day = today
            tonight = w != "today"
        elif w in _WEEKDAYS and day is None:
            day = today + timedelta(days=(_WEEKDAYS[w] - today.weekday()) % 7)
        else:
            rest.append(w)

    t = _parse_clock(" ".join(rest))
    if t is None:
        return None
    hour, minute, meridiem = t

    candidates: list[int]
    if meridiem == "am":
        candidates = [hour % 12]
    elif meridiem == "pm":
        candidates = [hour % 12 + 12]
    elif tonight and 1 <= hour <= 11:
        candidates = [hour + 12]
    elif day is None and 1 <= hour <= 11:
        candidates = [hour, hour + 12]  # whichever comes next
    else:
        candidates = [hour]

    def at(d: date, h: int) -> datetime:
        return _localize(datetime.combine(d, time(h, minute)), tz)

    cutoff = now - GRACE
    if day is None:
        options = [at(today, h) for h in candidates] + [
            at(today + timedelta(days=1), h) for h in candidates
        ]
        result = min(dt for dt in options if dt >= cutoff)
    else:
        result = at(day, candidates[0])
        # A weekday name that already passed today means next week.
        if result < cutoff and day == today and any(w in _WEEKDAYS for w in words):
            result = at(day + timedelta(days=7), candidates[0])
    return result.astimezone(UTC)


def _parse_clock(s: str) -> tuple[int, int, str | None] | None:
    s = s.strip()
    if s == "noon":
        return 12, 0, None
    if s == "midnight":
        return 0, 0, None
    m = _TIME_RE.match(s)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    meridiem = {"a": "am", "p": "pm"}.get(m.group(3), m.group(3))
    if minute >= 60 or (meridiem and not 1 <= hour <= 12) or hour > 23:
        return None
    return hour, minute, meridiem


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _localize(naive: datetime, tz: tzinfo) -> datetime:
    return naive.replace(tzinfo=tz)


def tz_label(tz: tzinfo, at: datetime) -> str:
    """Short name for a zone at a moment, e.g. 'EST', 'CEST' or 'UTC+05:30'."""
    name = at.astimezone(tz).tzname() or ""
    if not name or name[0] in "+-":
        offset = at.astimezone(tz).utcoffset() or timedelta(0)
        total = int(offset.total_seconds() // 60)
        sign = "-" if total < 0 else "+"
        h, mnt = divmod(abs(total), 60)
        return f"UTC{sign}{h:02d}:{mnt:02d}"
    return name


def format_local(dt: datetime, tz: tzinfo, now: datetime | None = None) -> str:
    """'Today 8:30 PM EST', 'Tomorrow 7:00 PM CET' or 'Fri Nov 20, 9:00 PM PST'."""
    now = (now or datetime.now(UTC)).astimezone(tz)
    local = dt.astimezone(tz)
    clock = local.strftime("%I:%M %p").lstrip("0")
    days = (local.date() - now.date()).days
    if days == 0:
        day = "Today"
    elif days == 1:
        day = "Tomorrow"
    elif days == -1:
        day = "Yesterday"
    else:
        day = local.strftime("%a %b ") + str(local.day) + ","
    return f"{day} {clock} {tz_label(tz, local)}"


def relative(dt: datetime, now: datetime | None = None) -> str:
    """'in 2h 15m', 'in 5m', 'now' or '20m ago'."""
    now = now or datetime.now(UTC)
    mins = round((dt - now).total_seconds() / 60)
    if abs(mins) < 1:
        return "now"
    h, m = divmod(abs(mins), 60)
    span = f"{h}h {m}m" if h and m else f"{h}h" if h else f"{m}m"
    if h >= 48:
        span = f"{round(abs(mins) / 1440)}d"
    return f"in {span}" if mins > 0 else f"{span} ago"


def time_suggestions(tz: tzinfo, now: datetime | None = None) -> list[tuple[str, str]]:
    """(label, value) quick picks. Values are re-parsed at submit in the chosen zone."""
    now = (now or datetime.now(UTC)).astimezone(tz)
    out = [
        ("Now", "now"),
        ("In 15 minutes", "in 15m"),
        ("In 30 minutes", "in 30m"),
        ("In 1 hour", "in 1h"),
        ("In 2 hours", "in 2h"),
    ]
    # Upcoming half-hour slots today, then evening slots tomorrow.
    slot = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end_today = now.replace(hour=23, minute=30, second=0, microsecond=0)
    while slot <= end_today and len(out) < 17:
        out.append((format_local(slot, tz, now), slot.strftime("%Y-%m-%d %H:%M")))
        slot += timedelta(minutes=30)
    tomorrow = now.date() + timedelta(days=1)
    for hour in range(17, 24):
        if len(out) >= 25:
            break
        local = _localize(datetime.combine(tomorrow, time(hour)), tz)
        out.append((format_local(local, tz, now), local.strftime("%Y-%m-%d %H:%M")))
    return out[:25]
