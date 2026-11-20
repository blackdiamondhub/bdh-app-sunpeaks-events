import datetime
import io
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from PIL import Image

from bdh_sdk import storage
from bdh_sdk.testing import FakeBroker

from . import ics, images, provides, refresh, scraper
from .plugin import SHARES

FIXTURES = Path(__file__).parent / "tests"
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 15, 12, tzinfo=UTC)


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def event(slug):
    return scraper.parse_event(fixture(f"{slug}.html"), slug)


def jpeg(size=(3000, 2000), exif=True):
    img = Image.new("RGB", size, (200, 40, 40))
    out = io.BytesIO()
    kwargs = {}
    if exif:
        ex = Image.Exif()
        ex[0x010F] = "TestCam"  # Make
        kwargs["exif"] = ex.tobytes()
    img.save(out, "JPEG", quality=90, **kwargs)
    return out.getvalue()


class Resp:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}


class FakeFetcher:
    """Serves fixture pages and generated pictures; records requests."""

    SLUGS = ("torchlight-parade", "artisan-market", "live-music-at-masas-bar-grill",
             "the-stoke-series-session-one", "no-image")

    def __init__(self, listing=None, broken=()):
        self.requests = []
        self.listing = listing
        self.broken = set(broken)
        self.image = jpeg()

    def text(self, url):
        self.requests.append(url)
        if url == scraper.LIST_URL:
            if self.listing is not None:
                return self.listing
            links = "".join(f'<a class="teaser-link" href="{scraper.EVENT_PATH}{s}">x</a>'
                            for s in self.SLUGS)
            select = fixture("list.html").split("<form>")[1].split("</form>")[0]
            return f"<html><body><form>{select}</form>{links}</body></html>"
        if url.startswith(scraper.LIST_URL + "?category="):
            if url.endswith("=397"):
                return fixture("list_397.html")
            return "<html></html>"
        slug = url.rsplit("/", 1)[1]
        if slug in self.broken:
            raise scraper.FetchError("HTTP 404")
        return fixture(f"{slug}.html")

    def get(self, url, headers=None):
        self.requests.append(url)
        if headers and headers.get("If-None-Match") == '"v1"':
            return Resp(304)
        return Resp(200, self.image, {"ETag": '"v1"'})


class ListParseTests(TestCase):
    def test_slugs_in_order_without_repeats(self):
        slugs = scraper.parse_list(fixture("list.html"))
        self.assertEqual(len(slugs), 39)
        self.assertEqual(len(set(slugs)), 39)
        self.assertIn("torchlight-parade", slugs)
        self.assertNotIn("not-ours", slugs)  # other hosts' links are ignored

    def test_categories(self):
        cats = dict(scraper.parse_categories(fixture("list.html")))
        self.assertEqual(cats["397"], "Live Music")
        self.assertEqual(cats["395"], "Food & Drink")
        self.assertNotIn("All", cats)


class DetailParseTests(TestCase):
    def test_timed_single_event(self):
        e = event("torchlight-parade")
        self.assertEqual(e.title, "Torchlight Parade")
        self.assertTrue(e.image_url.startswith("https://"))
        self.assertTrue(e.description)
        self.assertEqual(len(e.instances), 1)
        inst = e.instances[0]
        self.assertFalse(inst.all_day)
        self.assertEqual(inst.start.isoformat(), "2026-12-31T20:00:00-07:00")
        self.assertEqual(inst.end.isoformat(), "2026-12-31T21:00:00-07:00")

    def test_multi_day_all_day_with_schedule_and_free(self):
        e = event("artisan-market")
        self.assertEqual([(str(i.start), str(i.end)) for i in e.instances],
                         [("2026-12-11", "2026-12-13")])
        self.assertTrue(e.instances[0].all_day)
        self.assertEqual(e.price, "Free")
        self.assertIn("Sun Peaks Centre", e.location)
        self.assertEqual(e.schedule, [
            ("16:00", "19:00", "Friday December 11, 2026 — Featuring vendors in the Great Hall"),
            ("11:00", "17:00", "Saturday December 12, 2026"),
            ("10:00", "16:00", "Sunday December 13, 2026"),
        ])

    def test_recurring_dates(self):
        e = event("live-music-at-masas-bar-grill")
        self.assertEqual(len(e.instances), 10)
        self.assertEqual(len({i.start for i in e.instances}), 10)
        self.assertEqual(e.location, "Masa's Bar + Grill")

    def test_schedule_ranges_and_price(self):
        e = event("the-stoke-series-session-one")
        self.assertEqual(e.price, "$15 per athlete")
        self.assertEqual(e.location, "Base Camp Terrain Park")
        self.assertEqual(e.schedule[0], ("15:30", "16:30", "Check in and Bib Pick-up at the bottom of Base Camp"))
        self.assertEqual(len(e.schedule), 6)

    def test_schedule_single_times_and_ticket_options(self):
        e = event("gala")
        self.assertEqual(e.location, "Sun Peaks Grand Ballroom")
        self.assertTrue(e.price.startswith("Single Ticket: $149 plus GST; Table of 10"))
        self.assertEqual(e.schedule[0][:2], ("17:30", ""))
        self.assertIn("Later in the Evening: Live auction", e.schedule[1][2])

    def test_timed_range_over_days(self):
        e = event("bill-fell-photography-art-show-sale")
        self.assertTrue(e.price.startswith("By donation"))
        self.assertEqual(len(e.instances), 1)
        self.assertEqual((e.instances[0].end - e.instances[0].start).days, 1)

    def test_no_price_no_image(self):
        e = event("no-image")
        self.assertEqual(e.image_url, "")
        self.assertEqual(e.price, "")
        self.assertTrue(e.instances[0].all_day)
        self.assertIsNone(e.instances[0].end)

    def test_time_range_parsing(self):
        self.assertEqual(scraper.parse_time_range("3:30pm to 4:30pm Check in"),
                         ("15:30", "16:30", "Check in"))
        self.assertEqual(scraper.parse_time_range("12 a.m. – 1 AM"), ("00:00", "01:00", ""))
        self.assertEqual(scraper.parse_time_range("Doors at 7pm, show 8pm"),
                         ("19:00", "", "Doors at , show 8pm"))
        self.assertIsNone(scraper.parse_time_range("Later in the evening"))


class IcsTests(TestCase):
    def calendar(self, *slugs, images_=None):
        return ics.build_calendar([event(s) for s in slugs], images_, now=NOW).decode()

    def unfolded(self, text):
        return text.replace("\r\n ", "")

    def test_structure_and_folding(self):
        text = self.calendar("artisan-market", "the-stoke-series-session-one", "gala")
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        self.assertIn("X-WR-CALNAME:Sun Peaks events", text)
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line.encode()), 75, line)
        self.assertNotIn("\n", text.replace("\r\n", ""))

    def test_fold_is_utf8_safe(self):
        line = "SUMMARY:" + "é—" * 60
        folded = ics.fold(line)
        for part in folded.split("\r\n"):
            self.assertLessEqual(len(part.encode()), 75)
        self.assertEqual(folded.replace("\r\n ", ""), line)

    def test_escaping(self):
        self.assertEqual(ics.escape("a,b;c\\d\ne"), "a\\,b\\;c\\\\d\\ne")

    def test_all_day_range(self):
        text = self.unfolded(self.calendar("artisan-market", images_={"artisan-market": "artisan-market.webp"}))
        self.assertIn("UID:artisan-market-20261211@sunpeaks-events.bdh", text)
        self.assertIn("DTSTART;VALUE=DATE:20261211\r\nDTEND;VALUE=DATE:20261214", text)
        self.assertIn("IMAGE;VALUE=URI:bdh-share:artisan-market.webp", text)
        self.assertIn("X-BDH-PRICE:Free", text)
        self.assertIn("X-BDH-SCHEDULE:16:00–19:00|Friday December 11\\, 2026 — Featuring", text)

    def test_timed_in_utc_and_split_daily(self):
        text = self.unfolded(self.calendar("torchlight-parade", "bill-fell-photography-art-show-sale"))
        self.assertIn("DTSTART:20270101T030000Z\r\nDTEND:20270101T040000Z", text)
        self.assertIn("UID:bill-fell-photography-art-show-sale-20261218@", text)
        self.assertIn("UID:bill-fell-photography-art-show-sale-20261219@", text)
        self.assertIn("DTSTART:20261218T210000Z\r\nDTEND:20261219T030000Z", text)
        self.assertIn("DTSTART:20261219T210000Z\r\nDTEND:20261220T030000Z", text)
        self.assertNotIn("IMAGE", text)

    def test_recurring_and_stable_uids(self):
        a = self.calendar("live-music-at-masas-bar-grill")
        b = ics.build_calendar([event("live-music-at-masas-bar-grill")], now=NOW + datetime.timedelta(hours=1)).decode()
        self.assertEqual(ics.event_count(a.encode()), 10)
        uids = [line for line in a.split("\r\n") if line.startswith("UID:")]
        self.assertEqual(len(set(uids)), 10)
        self.assertEqual(uids, [line for line in b.split("\r\n") if line.startswith("UID:")])

    def test_past_occurrences_dropped(self):
        later = datetime.datetime(2027, 1, 1, tzinfo=UTC)
        text = ics.build_calendar([event("live-music-at-masas-bar-grill")], now=later).decode()
        self.assertEqual(ics.event_count(text.encode()), 4)  # Jan 2, 9, 16 and 23
        self.assertNotIn("live-music-at-masas-bar-grill-20261226", text)

    def test_categories(self):
        e = event("torchlight-parade")
        e.categories = ["Food & Drink", "Village, Plaza"]
        text = self.unfolded(ics.build_calendar([e], now=NOW).decode())
        self.assertIn("CATEGORIES:Food & Drink,Village\\, Plaza", text)


class ImageTests(TestCase):
    def test_resize_to_webp_without_metadata(self):
        out = images.to_webp(jpeg())
        with Image.open(io.BytesIO(out)) as img:
            self.assertEqual(img.format, "WEBP")
            self.assertEqual(max(img.size), images.MAX_EDGE)
            self.assertNotIn("exif", img.info)
            self.assertNotIn("icc_profile", img.info)
        self.assertLess(len(out), 200_000)

    def test_small_image_not_enlarged(self):
        with Image.open(io.BytesIO(images.to_webp(jpeg((800, 600), exif=False)))) as img:
            self.assertEqual(img.size, (800, 600))

    def test_cached_with_etag(self):
        fetcher = FakeFetcher()
        with tempfile.TemporaryDirectory() as d:
            first = images.fetch_image(fetcher, "a", "https://x/a.jpg", d)
            second = images.fetch_image(fetcher, "a", "https://x/a.jpg", d)
            self.assertEqual(first, second)
            self.assertTrue((Path(d) / "a.webp").exists())
            images.prune(d, [])
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_attachment_key(self):
        self.assertEqual(images.attachment_key("torchlight-parade"), "torchlight-parade.webp")
        self.assertLessEqual(len(images.attachment_key("x" * 100)), 64)


class RefreshTests(TestCase):
    def setUp(self):
        storage.set_state_backend(None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {k: v for k, v in os.environ.items() if k != "BDH_APP_ID"}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def broker(self):
        fb = FakeBroker(install=True, app_id="sunpeaks_events")
        fb.set_manifest_shares("sunpeaks_events", SHARES)
        return fb

    def run_refresh(self, fetcher):
        return refresh.refresh(fetcher, self.tmp.name, NOW)

    def test_publishes_calendar_with_images(self):
        with self.broker() as fb:
            st = self.run_refresh(FakeFetcher())
            revs = fb.share_revisions[("sunpeaks_events", "events")]
            self.assertEqual(st["revision"], 1)
            self.assertEqual(len(revs), 1)
            self.assertEqual(st["last_error"], "")
            self.assertEqual(st["events"], 5)
            self.assertEqual(st["images"], 4)  # no-image has none
            self.assertEqual(sorted(revs[0]["attachments"]), [
                "artisan-market.webp", "live-music-at-masas-bar-grill.webp",
                "the-stoke-series-session-one.webp", "torchlight-parade.webp"])
            main = fb.share_blobs[("sunpeaks_events", revs[0]["main"])]
            self.assertEqual(main["mime"], "text/calendar")
            text = main["data"].decode().replace("\r\n ", "")
            self.assertIn("CATEGORIES:Live Music", text)
            self.assertIn("IMAGE;VALUE=URI:bdh-share:torchlight-parade.webp", text)

            # Unchanged site: nothing republished, pictures served from cache.
            fetcher = FakeFetcher()
            st = self.run_refresh(fetcher)
            self.assertEqual(st["revision"], 1)
            self.assertEqual(len(fb.ops("shares.publish")), 1)

    def test_skips_broken_event_pages(self):
        with self.broker() as fb:
            st = self.run_refresh(FakeFetcher(broken={"torchlight-parade"}))
            self.assertEqual(st["events"], 4)
            self.assertEqual(st["skipped"], 1)
            self.assertEqual(len(fb.ops("shares.publish")), 1)

    def test_no_events_does_not_publish(self):
        with self.broker() as fb:
            st = self.run_refresh(FakeFetcher(listing="<html><body>Maintenance</body></html>"))
            self.assertIn("No events found", st["last_error"])
            self.assertEqual(fb.ops("shares.publish"), [])
            self.assertIsNone(st.get("revision"))

    def test_listing_failure_keeps_previous_revision(self):
        with self.broker() as fb:
            self.run_refresh(FakeFetcher())
            fetcher = FakeFetcher()
            fetcher.text = mock.Mock(side_effect=scraper.FetchError("HTTP 503"))
            st = self.run_refresh(fetcher)
            self.assertIn("HTTP 503", st["last_error"])
            self.assertEqual(st["revision"], 1)
            self.assertEqual(len(fb.ops("shares.publish")), 1)

    def test_staleness(self):
        self.assertTrue(refresh.is_stale(NOW))
        refresh._save(last_attempt=NOW.isoformat(), last_error="")
        self.assertFalse(refresh.is_stale(NOW + datetime.timedelta(hours=2)))
        self.assertTrue(refresh.is_stale(NOW + datetime.timedelta(hours=3)))
        refresh._save(last_error="boom")
        self.assertTrue(refresh.is_stale(NOW + datetime.timedelta(minutes=45)))


class OpeningDayTests(RefreshTests):
    """The resort's "Winter Opening Day" event becomes the hub's winter start."""

    def test_refresh_records_opening_day(self):
        with self.broker():  # the "no-image" page is Winter Opening Day
            st = self.run_refresh(FakeFetcher())
            hint = provides.season()
        self.assertEqual(st["opens_on"], "2026-11-21")
        self.assertEqual((hint.season, hint.opens_on), ("winter", datetime.date(2026, 11, 21)))

    def test_no_opening_event_means_no_hint(self):
        with self.broker():
            refresh._save(opens_on="2026-11-21")
            self.assertIsNotNone(provides.season())
            st = self.run_refresh(FakeFetcher(broken={"no-image"}))
            hint = provides.season()
        self.assertEqual(st["opens_on"], "")
        self.assertIsNone(hint)

    def test_opening_day_by_title_and_bad_dates(self):
        ev = scraper.Event("opening", " Winter Opening Day ", "u", instances=[
            scraper.Instance(datetime.datetime(2026, 11, 27, 9, tzinfo=UTC)),
            scraper.Instance(datetime.datetime(2026, 11, 26, 18, 30, tzinfo=UTC))])
        self.assertEqual(refresh.opening_day([event("gala"), ev]), datetime.date(2026, 11, 26))
        self.assertIsNone(refresh.opening_day([event("gala")]))
        self.assertIsNone(provides.build_season("not a date"))
        self.assertIsNone(provides.build_season(None))

    def test_declared_to_hub(self):
        from .plugin import spec
        self.assertEqual(spec.provides, {"season": "sunpeaks_events.provides.season"})


class ViewTests(TestCase):
    def setUp(self):
        storage.set_state_backend(None)
        refresh._save(last_attempt=datetime.datetime.now(UTC).isoformat(),
                      last_refresh=datetime.datetime.now(UTC).isoformat(),
                      events=3, entries=4, revision=2, last_error="")

    def test_page(self):
        r = self.client.get(reverse("sunpeaks_events"))
        self.assertContains(r, "Settings → Apps → Sun Peaks Events → Sharing")
        self.assertContains(r, "3 events")
        self.assertNotContains(r, "spe-refresh")

    def test_refresh_requires_admin(self):
        with mock.patch.object(refresh, "refresh_async", return_value=True) as start:
            r = self.client.post(reverse("sunpeaks_events_refresh"))
            self.assertEqual(r.status_code, 403)
            start.assert_not_called()
            admin = get_user_model().objects.create_superuser("admin", "a@example.com", "pw")
            self.client.force_login(admin)
            r = self.client.post(reverse("sunpeaks_events_refresh"))
            self.assertEqual(r.status_code, 200)
            start.assert_called_once()

    def test_status(self):
        from .homepage import status

        st = status()
        self.assertEqual(st.text, "4 upcoming events")
