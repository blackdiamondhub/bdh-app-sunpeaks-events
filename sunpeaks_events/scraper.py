"""Scrape Sun Peaks Resort's events listing and event pages.

Parsing is pure (HTML in, dataclasses out) so tests run on saved pages. The
site is Drupal: event dates come from its smart-date field (``<time
datetime=...>``), the rest from Open Graph tags and the body's details and
"Schedule" sections.
"""
import datetime
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

BASE_URL = "https://www.sunpeaksresort.com"
LIST_URL = BASE_URL + "/events-things-to-do/events"
EVENT_PATH = "/events-things-to-do/events/"
# The site rejects non-browser user agents.
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
TIMEOUT = 20
RETRIES = 3
DELAY = 0.5

_SLUG_RE = re.compile(r"^/events-things-to-do/events/([a-z0-9][a-z0-9-]*)/?$")
_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b\.?", re.I)
_RANGE_SEP_RE = re.compile(r"^\s*(?:-|–|—|to|until)\s*$", re.I)
_PRICE_LABELS = ("ticket price", "ticket prices", "tickets", "price", "prices", "cost", "admission",
                 "entry", "fee", "fees")
_PRICE_LIST_LABELS = ("ticket options", "ticket option", "pricing")
_LOCATION_LABELS = ("location", "where", "venue")
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_SKIP = ("ul", "ol", "script", "style", "picture", "img", "article") + _HEADINGS


class FetchError(Exception):
    pass


@dataclass
class Instance:
    """One occurrence. ``start``/``end`` are dates (all day) or aware datetimes."""

    start: object
    end: object = None

    @property
    def all_day(self):
        return not isinstance(self.start, datetime.datetime)


@dataclass
class Event:
    slug: str
    title: str
    url: str
    description: str = ""
    image_url: str = ""
    location: str = ""
    price: str = ""
    instances: list = field(default_factory=list)
    schedule: list = field(default_factory=list)  # [(start "HH:MM", end "HH:MM" or "", label)]
    categories: list = field(default_factory=list)


# --- fetching ---------------------------------------------------------------

class Fetcher:
    """Polite sequential HTTP: a browser-like User-Agent, a timeout, a few
    retries and a short pause between requests."""

    def __init__(self, session=None, delay=DELAY, sleep=time.sleep):
        import requests

        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.delay = delay
        self.sleep = sleep
        self._last = 0.0

    def get(self, url, headers=None):
        import requests

        error = None
        for attempt in range(RETRIES):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            try:
                resp = self.session.get(url, headers=headers or {}, timeout=TIMEOUT)
            except requests.RequestException as exc:
                error = exc
            else:
                if resp.status_code < 500 and resp.status_code != 429:
                    return resp
                error = f"HTTP {resp.status_code}"
                resp.close()
            finally:
                self._last = time.monotonic()
            if attempt + 1 < RETRIES:
                self.sleep(2 ** attempt)
        raise FetchError(f"{url}: {error}")

    def text(self, url):
        resp = self.get(url)
        if resp.status_code != 200:
            raise FetchError(f"{url}: HTTP {resp.status_code}")
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = "utf-8"
        return resp.text


# --- list page --------------------------------------------------------------

def _soup(html):
    return BeautifulSoup(html, "html.parser")


def _clean(text):
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def event_slug(href):
    url = urljoin(BASE_URL + "/", href or "")
    if not url.startswith(BASE_URL + "/"):
        return None
    m = _SLUG_RE.match(urlsplit(url).path)
    return m.group(1) if m else None


def parse_list(html):
    """Event slugs on the listing, in page order without repeats."""
    slugs = []
    for a in _soup(html).find_all("a", href=True):
        slug = event_slug(a["href"])
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs


def parse_categories(html):
    """``[(id, name)]`` from the listing's category filter."""
    select = _soup(html).find("select", attrs={"name": "category"})
    if not select:
        return []
    out = []
    for opt in select.find_all("option"):
        value = (opt.get("value") or "").strip()
        if value.isdigit():
            out.append((value, _clean(opt.get_text())))
    return out


# --- event page -------------------------------------------------------------

def _meta(soup, prop):
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return _clean(tag.get("content")) if tag else ""


def _parse_when(value):
    value = (value or "").strip()
    try:
        if len(value) == 10:
            return datetime.date.fromisoformat(value)
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def parse_instances(article):
    """Every occurrence on the page: the next one plus the "Upcoming Dates" list.

    Each occurrence is a ``<div>`` with one ``<time>`` (a single moment or
    day) or two (start and end). Repeats are dropped.
    """
    field_ = article.select_one(".field--name-field-date-recur")
    if not field_:
        return []
    out = []
    for time_tag in field_.find_all("time", attrs={"datetime": True}):
        holder = time_tag.parent
        times = holder.find_all("time", attrs={"datetime": True})
        if times[0] is not time_tag:
            continue  # the end time of an occurrence already read
        start = _parse_when(times[0]["datetime"])
        end = _parse_when(times[1]["datetime"]) if len(times) > 1 else None
        if start is None or (end is not None and type(end) is not type(start)):
            continue
        if end is not None and end < start:
            end = None
        inst = Instance(start, end)
        if inst not in out:
            out.append(inst)
    return out


def _lines(block):
    """Split a block at ``<br>``s into ``(strong, rest)`` text pairs.

    ``strong`` is the bold text a line starts with (usually its label) and
    ``rest`` everything after it. Nested lists and headings are skipped.
    """
    lines, strong, rest = [], [], []

    def flush():
        s, r = _clean("".join(strong)), _clean("".join(rest))
        if s or r:
            lines.append((s, r))
        strong.clear()
        rest.clear()

    def walk(node, bold):
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                if bold and not _clean("".join(rest)):
                    strong.append(str(child))
                else:
                    rest.append(str(child))
            elif isinstance(child, Tag):
                if child.name == "br":
                    flush()
                elif child.name not in _SKIP:
                    walk(child, bold or child.name in ("strong", "b"))

    walk(block, False)
    flush()
    return lines


def _label(strong, rest):
    """``("location", "Grand Ballroom")`` for "**Location:** Grand Ballroom"."""
    if not strong:
        m = re.match(r"^([A-Za-z][A-Za-z ]{1,24}):\s*(.+)$", rest)
        return (m.group(1).strip().lower(), m.group(2).strip()) if m else (None, rest)
    if strong.endswith(":"):
        return strong[:-1].strip().lower(), rest
    if rest.startswith(":"):
        return strong.strip().lower(), rest[1:].strip()
    if ":" in strong:
        label, _, value = strong.partition(":")
        return label.strip().lower(), _clean(value + " " + rest)
    return None, _clean(strong + " " + rest)


def _parse_details(body):
    """``(location, price)`` from the body's "**Label:** value" lines."""
    location = price = ""
    free = False
    for block in body.find_all(["p", "li"]):
        for strong, rest in _lines(block):
            label, value = _label(strong, rest)
            if label in _LOCATION_LABELS and value and not location:
                location = value
            elif label in _PRICE_LABELS and value and not price:
                price = value
            elif label in _PRICE_LIST_LABELS and not price:
                items = [value] if value else []
                lst = block.find_next_sibling(["ul", "ol"])
                if not items and lst is not None:
                    items = [_clean(li.get_text(" ")) for li in lst.find_all("li")]
                price = "; ".join(i for i in items if i)
            elif label is None and strong and not rest and re.search(r"\bfree\b", strong, re.I):
                free = True
    if not price and free:
        price = "Free"
    return location[:300], price[:300]


def _hhmm(match):
    hour, minute, ampm = int(match.group(1)), int(match.group(2) or 0), match.group(3).lower()
    if not (1 <= hour <= 12 and 0 <= minute < 60):
        return None
    hour = hour % 12 + (12 if ampm == "p" else 0)
    return f"{hour:02d}:{minute:02d}"


def parse_time_range(text):
    """``("15:30", "16:30", rest)`` for "3:30pm to 4:30pm …"; None if no time.

    ``rest`` is ``text`` without the times. The end is "" unless a second
    time follows the first with only a range separator between them.
    """
    text = text or ""
    first = _TIME_RE.search(text)
    start = _hhmm(first) if first else None
    if start is None:
        return None
    end, cut = "", first.end()
    second = _TIME_RE.search(text, first.end())
    if second and _RANGE_SEP_RE.match(text[first.end():second.start()]):
        end = _hhmm(second) or ""
        if end:
            cut = second.end()
    return start, end, _clean(text[:first.start()] + " " + text[cut:])


def _tidy_label(text):
    text = _clean(text).strip(" ,;:-–—|")
    return text.title() if text.isupper() else text


def _parse_schedule(body):
    """``[(start, end, label)]`` from the sections headed "…Schedule…".

    A row's time comes from its bold text (or the start of the line); a line
    without a time continues the previous row's label.
    """
    rows = []
    for heading in body.find_all(_HEADINGS):
        if "schedule" not in heading.get_text().lower():
            continue
        for sib in heading.find_next_siblings():
            if sib.name in _HEADINGS:
                break
            if sib.name in ("ul", "ol"):
                blocks = sib.find_all("li")
            elif sib.name in ("p", "div", "li"):
                blocks = [sib]
            else:
                continue
            for block in blocks:
                for strong, rest in _lines(block):
                    _schedule_line(rows, strong, rest)
    return [(s, e, label[:200]) for s, e, label in rows]


def _schedule_line(rows, strong, rest):
    parsed = parse_time_range(strong or rest)
    if parsed is None:
        text = _clean(f"{strong}: {rest}" if strong and rest else strong or rest)
        if text and not text.endswith(":") and rows:
            start, end, label = rows[-1]
            rows[-1] = (start, end, f"{label} — {text}" if label else text)
        return
    start, end, leftover = parsed
    label = (rest or leftover) if strong else leftover
    rows.append((start, end, _tidy_label(label)))


def parse_event(html, slug, url=None):
    """An :class:`Event` from its page, or None if the page has no title."""
    soup = _soup(html)
    article = soup.select_one("article.node-view-full") or soup
    title = _meta(soup, "og:title")
    if not title:
        h1 = soup.find("h1")
        title = _clean(h1.get_text()) if h1 else ""
    if not title:
        return None
    event = Event(slug=slug, title=title, url=url or f"{BASE_URL}{EVENT_PATH}{slug}")
    event.image_url = _meta(soup, "og:image")
    body = article.select_one(".field--name-body")
    description = _meta(soup, "og:description")
    if not description and body is not None:
        first = body.find("p")
        description = _clean(first.get_text(" ")) if first else ""
    extra = article.select_one(".field--name-field-additional-info")
    extra = _clean(extra.get_text(" ")) if extra else ""
    if extra and extra not in description:
        description = f"{description}\n\n{extra}" if description else extra
    event.description = description
    event.instances = parse_instances(article)
    if body is not None:
        event.location, event.price = _parse_details(body)
        event.schedule = _parse_schedule(body)
    return event


# --- whole site -------------------------------------------------------------

def scrape(fetcher, log=None):
    """Fetch the listing and every event page. Raises :class:`FetchError` if
    the listing can't be read; events whose page fails are skipped."""
    listing = fetcher.text(LIST_URL)
    slugs = parse_list(listing)
    categories = {}
    for cat_id, name in parse_categories(listing):
        try:
            for slug in parse_list(fetcher.text(f"{LIST_URL}?category={cat_id}")):
                categories.setdefault(slug, []).append(name)
        except FetchError as exc:
            if log:
                log(f"category {name}: {exc}")
    events, failures = [], []
    for slug in slugs:
        url = f"{BASE_URL}{EVENT_PATH}{slug}"
        try:
            event = parse_event(fetcher.text(url), slug, url)
        except Exception as exc:  # one bad page must not sink the feed
            failures.append(f"{slug}: {exc}")
            continue
        if event is None or not event.instances:
            failures.append(f"{slug}: no title or dates")
            continue
        event.categories = categories.get(slug, [])
        events.append(event)
    return events, failures
