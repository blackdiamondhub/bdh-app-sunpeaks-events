"""Assistant tool: what's on at Sun Peaks, for the hub's voice/chat assistant.

One read-only lookup for every mode. Each refresh saves a compact copy of the
scraped events (see :func:`snapshot`); the tool picks the ones in the asked
window (today, tonight, tomorrow, a weekday, the weekend, a date or the next
few days), optionally matching some words, and answers with a few
speech-ready strings: times like "8 pm" in lodge time (UTC-7 all year).
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

from bdh_sdk import ALL_MODES
from bdh_sdk.storage import app_state
from bdh_sdk.tools import Tool, ToolError

EVENTS = "events"
LODGE = dt.timezone(dt.timedelta(hours=-7))
LOADING = "The Sun Peaks events list is still loading; ask again in a minute."
STALE = "The Sun Peaks events list hasn't updated in {days} days, so I can't say what's on."
STALE_NOTE = "The events list was last updated {days} days ago."
NOTE_AFTER = dt.timedelta(days=2)
STALE_AFTER = dt.timedelta(days=14)
MAX_EVENTS = 5
MAX_TIMES = 3
ABOUT_CHARS = 160
NEXT_WITHIN = dt.timedelta(days=60)
# Evening starts at 5 pm; "tonight" runs until 4 am.
EVENING = dt.time(17)
NIGHT_ENDS = dt.timedelta(hours=28)
# An all-day event with no hours counts as on between these.
DAY_STARTS, DAY_ENDS = dt.timedelta(hours=9), dt.timedelta(hours=17)
PERIODS = {"morning": (6, 12), "afternoon": (12, 17), "evening": (17, 28), "night": (17, 28)}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                          "sep", "oct", "nov", "dec"))}
_FILLER = re.compile(r"\b(this|the|on|next|coming|for|at|in|of)\b")
QUERY_FILLER = frozenset("a an and any anything are at events event for in is of on or something "
                         "the there what whats happening things to with".split())


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def tools(mode=None):
    return [
        Tool(EVENTS, "Sun Peaks events",
             "What's on at Sun Peaks Resort: events like concerts, live music, parades, markets, races "
             "and festivals, with times and venues, for tonight, a day, the weekend or the next few days. "
             "Not for weather, lifts or restaurant hours.",
             params={"type": "object", "properties": {
                 "day": {"type": "string", "maxLength": 40,
                         "description": "When, in the guest's words: today, tonight, tomorrow, tomorrow night, a "
                                        "weekday (saturday), weekend, this week, or a date like December 31. "
                                        "Leave out for today and tonight."},
                 "days": {"type": "integer", "minimum": 1, "maximum": 14,
                          "description": "How many days from today (1-14) when no day is given."},
                 "query": {"type": "string", "maxLength": 60,
                           "description": "Only events matching these words (title, venue or kind): music, "
                                          "kids, market, fireworks, Masa's."},
             }},
             modes=ALL_MODES, readonly=True),
    ]


# ── saved events ──

def _iso(value):
    return value.isoformat() if value is not None else None


def snapshot(events, now):
    """The scraped events as plain data for :func:`run_tool`, past ones dropped."""
    from . import ics

    out = []
    for event in events:
        times = [[_iso(start), _iso(end)] for start, end in ics.occurrences(event)
                 if not ics._ended(start, end, now)]
        if not times:
            continue
        out.append({"title": event.title.strip(), "where": event.location, "price": event.price,
                    "about": _short(event.description), "kinds": list(event.categories),
                    "times": times, "schedule": [list(row) for row in event.schedule]})
    return {"saved": now.isoformat(), "events": out}


def save(events, now):
    from .refresh import APP_ID

    app_state(APP_ID).set(EVENTS, snapshot(events, now))


def _saved():
    from .refresh import APP_ID

    return app_state(APP_ID).get(EVENTS)


def _short(text, limit=ABOUT_CHARS):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "..."


# ── when ──

def _weekday(word):
    word = word.rstrip(".")
    if len(word) < 3:
        return None
    return next((i for i, w in enumerate(WEEKDAYS) if w == word.rstrip("s") or w.startswith(word)), None)


def _parse_date(text, today):
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return dt.date(*map(int, m.groups()))
    m = (re.fullmatch(r"(?:[a-z]+ )?([a-z]+)\.? (\d{1,2})(?:st|nd|rd|th)?", text)
         or re.fullmatch(r"(?:[a-z]+ )?(\d{1,2})(?:st|nd|rd|th)? ([a-z]+)", text))
    if m:
        month, day = m.groups() if m.group(1).isalpha() else m.groups()[::-1]
        month = MONTHS.get(month[:3])
        if month:
            try:
                d = dt.date(today.year, month, int(day))
            except ValueError:
                return None
            return d if d >= today - dt.timedelta(days=1) else d.replace(year=today.year + 1)
    return None


def _midnight(d):
    return dt.datetime.combine(d, dt.time())


def _day_label(d, today):
    if d == today:
        return "today"
    if d == today + dt.timedelta(days=1):
        return "tomorrow"
    if today < d < today + dt.timedelta(days=7):
        return f"{d:%A}"
    return f"{d:%A} {d:%B} {d.day}"


def window(day, days, local):
    """``(start, end, label)`` in lodge time (naive) for the tool's arguments; raises ToolError."""
    today = local.date()
    if days and not day:
        return local, _midnight(today + dt.timedelta(days=days)), (
            "today" if days == 1 else f"the next {days} days")
    s = " ".join(_FILLER.sub(" ", (day or "").lower().replace("'", "").strip(" ?.!,")).split())
    if s in ("", "now", "right now", "today and tonight", "later", "later today", "rest of today"):
        return local, _midnight(today) + NIGHT_ENDS, "today"
    if s in ("today", "today day"):
        return local, _midnight(today + dt.timedelta(days=1)), "today"
    if s in ("week", "this week", "next few days", "few days", "next week", "coming days"):
        return local, _midnight(today + dt.timedelta(days=7)), "the next 7 days"
    if s in ("weekend", "weekend days", "saturday and sunday", "sat and sun"):
        saturday = today + dt.timedelta(days=(5 - today.weekday()) % 7)
        if today.weekday() == 6:
            saturday = today - dt.timedelta(days=1)
        friday_night = _midnight(saturday) - dt.timedelta(hours=7)
        return max(local, friday_night), _midnight(saturday + dt.timedelta(days=2)), "this weekend"
    if s in ("tonight", "today night", "today evening", "evening", "night", "tonite"):
        s = "today night"
    period = next((w for w in s.split() if w in PERIODS), None)
    if period:
        s = " ".join(w for w in s.split() if w != period)
    if s in ("", "today"):
        d = today
    elif s == "tomorrow":
        d = today + dt.timedelta(days=1)
    elif s in ("day after tomorrow", "day after"):
        d = today + dt.timedelta(days=2)
    elif _weekday(s) is not None:
        d = today + dt.timedelta(days=(_weekday(s) - today.weekday()) % 7)
    else:
        d = _parse_date(s, today)
        if d is None:
            raise ToolError(f"I can't tell which day {day!r} is; say today, tonight, tomorrow, a weekday, "
                            "the weekend or a date.")
    label = _day_label(d, today)
    if period:
        first, last = PERIODS[period]
        start, end = _midnight(d) + dt.timedelta(hours=first), _midnight(d) + dt.timedelta(hours=last)
        if label == "today":
            label = "tonight" if first >= 17 else f"this {period}"
        else:
            label = f"{label} {period}"
    else:
        start, end = _midnight(d), _midnight(d + dt.timedelta(days=1))
    if end <= local:
        raise ToolError(f"{label.capitalize()} is already over; ask about tonight, tomorrow or a weekday.")
    return max(start, local), end, label


# ── occurrences ──

def _local(value):
    """A saved ISO time as lodge time (naive datetime) or a date."""
    if value is None:
        return None
    if len(value) == 10:
        return dt.date.fromisoformat(value)
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(LODGE).replace(tzinfo=None)
    return parsed


def _day_hours(event, d):
    """``(start, end)`` for day ``d`` of an all-day event from its schedule rows, or None."""
    needle = f"{d:%B} {d.day}".lower()
    for start, end, label in event.get("schedule") or ():
        if re.search(rf"\b{needle}\b", label.lower()):
            begin = _midnight(d) + dt.timedelta(hours=int(start[:2]), minutes=int(start[3:5]))
            finish = None
            if end:
                finish = _midnight(d) + dt.timedelta(hours=int(end[:2]), minutes=int(end[3:5]))
                if finish <= begin:
                    finish += dt.timedelta(days=1)
            return begin, finish
    return None


def occurrences(event, start, end):
    """``[(start, end, all_day)]`` of ``event`` overlapping the window, in time order."""
    out = []
    for raw_start, raw_end in event["times"]:
        first, last = _local(raw_start), _local(raw_end)
        if isinstance(first, dt.datetime):
            finish = last or first + dt.timedelta(hours=1)
            if first < end and finish > start:
                out.append((first, last, False))
            continue
        last = last or first
        d = max(first, start.date())
        while d <= last and _midnight(d) < end:
            hours = _day_hours(event, d)
            if hours:
                finish = hours[1] or hours[0] + dt.timedelta(hours=1)
                if hours[0] < end and finish > start:
                    out.append((hours[0], hours[1], False))
            elif start < _midnight(d) + DAY_ENDS and end > _midnight(d) + DAY_STARTS:
                # All day: counts for a day's (or the rest of today's) question, not for tonight.
                out.append((_midnight(d), None, True))
            d += dt.timedelta(days=1)
    return sorted(out, key=lambda o: o[0])


# ── speaking ──

def _clock(t):
    if t.hour == 0 and t.minute == 0:
        return "midnight"
    if t.hour == 12 and t.minute == 0:
        return "noon"
    hour = t.hour % 12 or 12
    suffix = "am" if t.hour < 12 else "pm"
    return f"{hour}:{t.minute:02d} {suffix}" if t.minute else f"{hour} {suffix}"


def _day_of(t, local):
    """The day an event at ``t`` is said to be on: early-morning hours belong to the night before."""
    d = t.date()
    if t.hour < 4 and isinstance(t, dt.datetime):
        d -= dt.timedelta(days=1)
    label = _day_label(d, local.date())
    if label == "today" and t.time() >= EVENING:
        return "tonight"
    return label


def speak(occurrence, local, with_day=True):
    first, last, all_day = occurrence
    if all_day:
        return f"all day {_day_label(first.date(), local.date())}" if with_day else "all day"
    day = f"{_day_of(first, local)} " if with_day else ""
    if first <= local and last and last > local:
        return f"on now until {_clock(last)}" if last - local < dt.timedelta(hours=20) else \
            f"on now until {_day_label(last.date(), local.date())} {_clock(last)}"
    if last is None:
        return f"{day}at {_clock(first)}"
    if last - first > dt.timedelta(hours=20):
        return f"{day}{_clock(first)} to {_day_label(last.date(), local.date())} {_clock(last)}"
    return f"{day}{_clock(first)} to {_clock(last)}"


# ── matching ──

def _fold(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text.replace("'", "")).strip()


def _stem(word):
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _terms(query):
    return [_stem(w) for w in _fold(query).split() if w not in QUERY_FILLER]


def _matches(event, terms, fields):
    words = {_stem(w) for f in fields for w in _fold(event.get(f) if f != "kinds" else
                                                     " ".join(event.get("kinds") or ())).split()}
    text = " ".join(sorted(words))
    return all(t in words or (len(t) > 3 and t in text) for t in terms)


def _filter(events, query):
    terms = _terms(query or "")
    if not terms:
        return events
    for fields in (("title", "where", "kinds"), ("title", "where", "kinds", "about")):
        found = [e for e in events if _matches(e, terms, fields)]
        if found:
            return found
    return []


# ── the tool ──

def _entry(event, times, local, detail):
    out = {"title": event["title"]}
    shown = [speak(o, local) for o in times[:MAX_TIMES]]
    if len(times) > MAX_TIMES:
        shown.append(f"and {len(times) - MAX_TIMES} more times")
    out["when"] = ", ".join(shown)
    if event.get("where"):
        out["where"] = event["where"]
    if detail:
        if event.get("price"):
            out["price"] = _short(event["price"], 80)
        if event.get("about"):
            out["about"] = event["about"]
    return out


def _events(args, now):
    data = _saved()
    try:
        from .refresh import maybe_refresh_async, refresh_async

        (maybe_refresh_async if data else refresh_async)()
    except Exception:
        pass
    if not data or not data.get("saved"):
        raise ToolError(LOADING)
    age = now - dt.datetime.fromisoformat(data["saved"])
    if age > STALE_AFTER:
        raise ToolError(STALE.format(days=age.days))
    local = now.astimezone(LODGE).replace(tzinfo=None)
    start, end, label = window((args.get("day") or "").strip(), args.get("days"), local)
    events = _filter(data.get("events") or (), args.get("query"))
    query = " ".join((args.get("query") or "").split())
    found = []
    for event in events:
        times = occurrences(event, start, end)
        if times:
            found.append((times[0][0], event, times))
    found.sort(key=lambda f: (f[0], f[1]["title"]))
    matching = f" matching {query}" if query else ""
    out = {"when": label}
    if found:
        out["found"] = f"{len(found)} event{'' if len(found) == 1 else 's'}{matching}"
        detail = len(found) <= 3
        out["events"] = [_entry(e, t, local, detail) for _, e, t in found[:MAX_EVENTS]]
        if len(found) > MAX_EVENTS:
            out["also"] = ", ".join(e["title"] for _, e, _ in found[MAX_EVENTS:MAX_EVENTS + 8])
    else:
        span = f"over {label}" if label.startswith("the next") else label
        out["found"] = f"Nothing{matching} on the Sun Peaks events calendar {span}."
        upcoming = []
        for event in events:
            later = occurrences(event, end, end + NEXT_WITHIN)
            if later:
                upcoming.append((later[0][0], event, later[0]))
        if upcoming:
            first, event, occurrence = min(upcoming, key=lambda u: (u[0], u[1]["title"]))
            out["next"] = f"{event['title']}, {speak(occurrence, local)}" + (
                f", at {event['where']}" if event.get("where") else "")
    if age > NOTE_AFTER:
        out["note"] = STALE_NOTE.format(days=age.days)
    return out


def run_tool(tool_id, args, mode=None):
    if tool_id != EVENTS:
        raise ToolError(f"Unknown tool {tool_id!r}")
    return _events(args or {}, utc_now())
