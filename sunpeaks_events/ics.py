"""Build the RFC 5545 calendar.

Timed occurrences are written in UTC (``...Z``) from the site's explicit
offsets, so the feed needs no VTIMEZONE; all-day occurrences are DATE values
with an exclusive end. Besides the standard properties each event carries
``IMAGE;VALUE=URI:bdh-share:<key>`` (the image attached to the share),
``X-BDH-PRICE`` (free-text price) and one ``X-BDH-SCHEDULE`` per schedule row
(``HH:MM–HH:MM|Label`` or ``HH:MM|Label``, 24-hour local time).
"""
import datetime

PRODID = "-//BlackDiamondLodge//sunpeaks-events//EN"
CALNAME = "Sun Peaks events"
TZID = "America/Vancouver"
UID_DOMAIN = "sunpeaks-events.bdh"
# A timed occurrence spanning up to this many days, ending later in the day
# than it starts, is a daily event ("Dec 18-19, 2pm-8pm") and is split per day.
MAX_SPLIT_DAYS = 14
# Occurrences that ended more than this long ago are dropped.
KEEP_PAST = datetime.timedelta(days=1)


def escape(text):
    return (str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def fold(line):
    """Fold a content line to at most 75 octets per physical line (UTF-8 safe)."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    parts, chunk, size, limit = [], [], 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            parts.append("".join(chunk))
            chunk, size, limit = [], 0, 74  # continuation lines start with a space
        chunk.append(ch)
        size += n
    parts.append("".join(chunk))
    return "\r\n ".join(parts)


def _utc(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _date(d):
    return d.strftime("%Y%m%d")


def occurrences(event):
    """``[(start, end)]`` to write: all-day ranges kept whole, short timed
    multi-day ranges split into one occurrence per day."""
    out = []
    for inst in event.instances:
        start, end = inst.start, inst.end
        if inst.all_day:
            out.append((start, end or start))
            continue
        if end is None:
            out.append((start, None))
            continue
        days = (end.date() - start.date()).days
        if 0 < days <= MAX_SPLIT_DAYS and end.timetz().replace(tzinfo=None) > \
                start.timetz().replace(tzinfo=None):
            for i in range(days + 1):
                day_start = start + datetime.timedelta(days=i)
                day_end = end - datetime.timedelta(days=days - i)
                out.append((day_start, day_end))
        else:
            out.append((start, end))
    return out


def _ended(start, end, now):
    if isinstance(start, datetime.datetime):
        last = end or start
        return last < now - KEEP_PAST
    return datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time(),
                                     tzinfo=now.tzinfo) < now - KEEP_PAST


def build_calendar(events, images=None, now=None):
    """The calendar as bytes. ``images`` maps slug to the attachment key of
    its picture; events without one get no IMAGE line."""
    images = images or {}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = _utc(now)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{escape(CALNAME)}", f"X-WR-TIMEZONE:{TZID}"]
    uids = set()
    for event in events:
        for start, end in occurrences(event):
            if _ended(start, end, now):
                continue
            uid = f"{event.slug}-{_date(start)}"
            if uid in uids and isinstance(start, datetime.datetime):
                uid += start.strftime("-%H%M")
            n, base = 2, uid
            while uid in uids:
                uid, n = f"{base}-{n}", n + 1
            uids.add(uid)
            lines += ["BEGIN:VEVENT", f"UID:{uid}@{UID_DOMAIN}", f"DTSTAMP:{stamp}"]
            if isinstance(start, datetime.datetime):
                lines.append(f"DTSTART:{_utc(start)}")
                if end is not None:
                    lines.append(f"DTEND:{_utc(end)}")
            else:
                lines.append(f"DTSTART;VALUE=DATE:{_date(start)}")
                lines.append(f"DTEND;VALUE=DATE:{_date(end + datetime.timedelta(days=1))}")
            lines.append(f"SUMMARY:{escape(event.title)}")
            if event.description:
                lines.append(f"DESCRIPTION:{escape(event.description)}")
            if event.location:
                lines.append(f"LOCATION:{escape(event.location)}")
            lines.append(f"URL:{event.url}")
            if event.categories:
                lines.append("CATEGORIES:" + ",".join(escape(c) for c in event.categories))
            if images.get(event.slug):
                lines.append(f"IMAGE;VALUE=URI:bdh-share:{images[event.slug]}")
            if event.price:
                lines.append(f"X-BDH-PRICE:{escape(event.price)}")
            for row_start, row_end, label in event.schedule:
                when = f"{row_start}–{row_end}" if row_end else row_start
                lines.append(f"X-BDH-SCHEDULE:{escape(when + '|' + label)}")
            lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(fold(line) for line in lines) + "\r\n").encode("utf-8")


def event_count(ics_bytes):
    return ics_bytes.count(b"BEGIN:VEVENT")
