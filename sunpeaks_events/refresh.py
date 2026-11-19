"""Refresh the feed: scrape, build the calendar, publish the share.

Refreshes run in a background thread (the hub gives status hooks a few
seconds) and one at a time. If the site can't be read or yields no events,
nothing is published, so the previous revision stays live, and the error is
recorded for the page.
"""
import datetime
import hashlib
import logging
import re
import threading

from bdh_sdk import shares
from bdh_sdk.storage import app_state

from . import ics, images, scraper

log = logging.getLogger(__name__)

APP_ID = "sunpeaks_events"
SHARE = "events"
STATE_KEY = "status"
# Refresh when the last attempt is older than this (the hub wakes the app
# every three hours) ...
STALE_AFTER = datetime.timedelta(hours=2, minutes=30)
# ... or sooner after a failed one.
RETRY_AFTER = datetime.timedelta(minutes=30)
# The resort's opening-day event, offered to the hub as the start of winter.
OPENING_SLUG = "winter-opening-day"
OPENING_TITLE = "winter opening day"

_lock = threading.Lock()


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def state():
    return app_state(APP_ID).get(STATE_KEY) or {}


def _save(**changes):
    st = state()
    st.update(changes)
    app_state(APP_ID).set(STATE_KEY, st)
    return st


def is_running():
    return _lock.locked()


def is_stale(now=None):
    st = state()
    last = st.get("last_attempt")
    if not last:
        return True
    try:
        last = datetime.datetime.fromisoformat(last)
    except ValueError:
        return True
    age = (now or _now()) - last
    return age > (RETRY_AFTER if st.get("last_error") else STALE_AFTER)


def opening_day(events):
    """First date of the resort's opening-day event, or None."""
    for event in events:
        if event.slug == OPENING_SLUG or event.title.strip().lower() == OPENING_TITLE:
            starts = [i.start for i in event.instances]
            if starts:
                first = min(starts)
                return first.date() if isinstance(first, datetime.datetime) else first
    return None


def _feed_hash(ics_bytes, attachments):
    """Hash of the feed ignoring DTSTAMP, so an unchanged site isn't republished."""
    h = hashlib.sha256(re.sub(rb"\r\nDTSTAMP:[^\r]*", b"", ics_bytes))
    for key in sorted(attachments):
        h.update(key.encode() + hashlib.sha256(attachments[key][0]).digest())
    return h.hexdigest()


def refresh(fetcher=None, image_dir=None, now=None):
    """Run one refresh now (blocking). Returns the saved status, or None if
    another refresh is already running."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        return _refresh(fetcher or scraper.Fetcher(), image_dir, now or _now())
    finally:
        _lock.release()


def _refresh(fetcher, image_dir, now):
    _save(last_attempt=now.isoformat())
    try:
        events, failures = scraper.scrape(fetcher, log=log.warning)
    except Exception as exc:
        log.exception("sunpeaks_events: listing failed")
        return _save(last_error=f"Couldn't read the events page: {exc}")
    for failure in failures:
        log.warning("sunpeaks_events: skipped %s", failure)
    if not events:
        return _save(last_error="No events found on the events page; the site may have changed.")
    day = opening_day(events)
    _save(opens_on=day.isoformat() if day else "")

    attachments, keys, sizes = {}, {}, []
    for event in events:
        try:
            webp = images.fetch_image(fetcher, event.slug, event.image_url, image_dir)
        except Exception as exc:  # a bad picture only loses the picture
            log.warning("sunpeaks_events: image for %s: %s", event.slug, exc)
            webp = None
        if webp:
            key = images.attachment_key(event.slug)
            attachments[key] = (webp, images.MIME)
            keys[event.slug] = key
            sizes.append(len(webp))
    try:
        images.prune(image_dir, list(attachments))
    except OSError:
        pass

    calendar = ics.build_calendar(events, keys, now=now)
    count = ics.event_count(calendar)
    if count == 0:
        return _save(last_error="No upcoming events found on the events page.")
    stats = {
        "events": len(events),
        "entries": count,
        "skipped": len(failures),
        "images": len(sizes),
        "image_bytes": sum(sizes),
        "image_max_bytes": max(sizes, default=0),
    }
    digest = _feed_hash(calendar, attachments)
    st = state()
    if digest == st.get("hash") and st.get("revision") and not st.get("last_error"):
        return _save(last_refresh=now.isoformat(), last_error="", **stats)
    try:
        revision = shares.publish(SHARE, calendar, attachments=attachments)
    except Exception as exc:  # SharesError, or the broker being unreachable
        log.warning("sunpeaks_events: publish failed: %s", exc)
        return _save(last_error=f"Couldn't publish the feed: {exc}", **stats)
    return _save(last_refresh=now.isoformat(), last_error="", revision=revision, hash=digest,
                 **stats)


def refresh_async():
    """Start a refresh in the background. False if one is already running."""
    if is_running():
        return False

    def run():
        try:
            refresh()
        except Exception:
            log.exception("sunpeaks_events: refresh crashed")
        finally:
            try:
                from django.db import connection

                connection.close()
            except Exception:
                pass

    threading.Thread(target=run, name="sunpeaks-events-refresh", daemon=True).start()
    return True


def maybe_refresh_async():
    """Start a background refresh if the feed is stale."""
    if is_stale() and not is_running():
        return refresh_async()
    return False
