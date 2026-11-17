from django.core.management.base import BaseCommand

from sunpeaks_events import refresh


class Command(BaseCommand):
    help = "Scrape Sun Peaks events now and publish the calendar share."

    def handle(self, *args, **options):
        st = refresh.refresh()
        if st is None:
            self.stderr.write("A refresh is already running.")
            return
        if st.get("last_error"):
            self.stderr.write(st["last_error"])
        self.stdout.write(
            f"{st.get('events', 0)} events, {st.get('entries', 0)} calendar entries, "
            f"{st.get('images', 0)} images ({st.get('image_bytes', 0)} bytes), "
            f"revision {st.get('revision')}")
