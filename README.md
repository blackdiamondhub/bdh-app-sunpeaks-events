# bdh-app-sunpeaks-events

Black Diamond Hub app: **Sun Peaks Events**. It turns the events listed on
[sunpeaksresort.com](https://www.sunpeaksresort.com/events-things-to-do/events)
into a calendar feed that other hub apps can read through app sharing
(`bdh_sdk.shares`, bdh-sdk 0.35+). App id, package and URL: `sunpeaks_events`.

To show the events in the hub's calendar, share the feed with Calendar in
**Settings → Apps → Sun Peaks Events → Sharing**.

## What it does

1. Fetches the events list, then the list once per category
   (`?category=<id>`, to learn each event's categories), then every event's
   page. Requests go one at a time with a browser-like `User-Agent`, a short
   pause between them, a 20 s timeout and up to 3 tries.
2. Reads each event page: `og:title`, `og:description`, `og:image`, the
   Details block (Date, Time, Location, Ticket Price) and the optional
   "Schedule of Events".
3. Downloads each event's picture and converts it to WebP (longest edge at
   most 1600 px, quality 80, no metadata). Pictures are cached in the app's
   data directory (`/data/sunpeaks_events/images`) and revalidated with
   `If-None-Match`/`If-Modified-Since`, so unchanged ones aren't downloaded
   or converted again. Pictures are handled one at a time.
4. Builds the `.ics` and publishes it as the share `events`
   (`text/calendar`, "Sun Peaks events") with the pictures as attachments.
   The SDK uploads only blobs the hub doesn't already hold. If the feed is
   unchanged (ignoring `DTSTAMP`) nothing is published.

If the list can't be read, or no events are found, nothing is published: the
previous revision stays live and the error shows on the app page. Event pages that fail are skipped (counted as "skipped").

The app is `on_demand` with `wake_every` 3 hours. It refreshes in a
background thread when it wakes (from the status hook or the page) and the
last attempt is older than 2.5 hours, or 30 minutes after a failed one.

## Feed format

RFC 5545, CRLF line endings, lines folded at 75 octets (never inside a UTF-8
character). Calendar properties: `PRODID:-//BlackDiamondLodge//sunpeaks-events//EN`,
`X-WR-CALNAME:Sun Peaks events`, `X-WR-TIMEZONE:America/Vancouver`.

**Times.** Timed events are written in UTC (`DTSTART:20261231T030000Z`),
with no `VTIMEZONE`. The site gives every time with an explicit offset
(`-07:00`), and BC stays on UTC-7 all year from November 2026, so UTC is
exact and spares readers the tzdata question. All-day events use
`VALUE=DATE`, with an exclusive `DTEND` (the day after the last day).

Each listed date becomes its own `VEVENT`. A timed range over several days
(e.g. "Dec 18 2 pm – Dec 19 8 pm", up to 14 days) is split into one
`VEVENT` per day with the daily hours. Occurrences that ended more than a day
ago are dropped.

Per `VEVENT`:

| Property | Value |
| --- | --- |
| `UID` | `<slug>-<YYYYMMDD>[-<HHMM>]@sunpeaks-events.bdh`, stable across refreshes |
| `SUMMARY` / `DESCRIPTION` | title / plain-text description |
| `LOCATION` | from the Details block, when given |
| `URL` | the event's page |
| `CATEGORIES` | the site's categories, comma-separated |
| `IMAGE;VALUE=URI:bdh-share:<slug>.webp` | the picture, an attachment of the share |
| `X-BDH-PRICE` | free-text price, e.g. `Free` or `$15 per athlete` |
| `X-BDH-SCHEDULE` | one per schedule row, in order: `HH:MM–HH:MM\|Label` or `HH:MM\|Label` (24 h, en dash) |

Values are escaped per RFC 5545 (`\\`, `\,`, `\;`, `\n`).

## Page (no tile)

This is a background app: it has no home-screen tile. The hub's waker runs
its status hook every `wake_every` (3 h), which wakes it to refresh.

`/sunpeaks_events/` shows the last refresh, the number of events, calendar
entries and pictures, the share revision and the last error. Admins get a
**Refresh now** button.

## Container (hub API 2)

No broker permissions (sharing needs none), `network: outbound`
(sunpeaksresort.com), `lifecycle: on_demand`, `wake_every: 3 h`,
`memory_mb: 160`. A full refresh (39 events, 64 entries, 39 pictures) peaks
at about 110 MB RSS on its own and 145 MB with the test broker in the same
process; Pillow decoding the 1800 × 1200 JPEGs is the largest step.

## Development

CI uses the reusable `app-ci.yml` from bdh-sdk: it builds the wheel,
installs it next to the hub, runs `hub_smoke` and the tests, and builds and
smoke-tests the container image. The tests run offline against saved pages
in `sunpeaks_events/tests/` and publish to `bdh_sdk.testing.FakeBroker`.

`python manage.py refresh_sunpeaks_events` runs a refresh in the
foreground and prints the result.

Bump `version` in `pyproject.toml` and merge to `main` to release: the
release workflow tags `v<version>`, and the catalog picks the release up.
