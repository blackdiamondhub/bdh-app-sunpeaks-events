from bdh_sdk import PluginSpec, Tile
from bdh_sdk.plugins import LIFECYCLE_ON_DEMAND, NETWORK_OUTBOUND

SHARES = (
    {"name": "events", "type": "text/calendar", "title": "Sun Peaks events"},
)

spec = PluginSpec(
    id="sunpeaks_events",
    app="sunpeaks_events",
    urls="sunpeaks_events.urls",
    status="sunpeaks_events.homepage.status",
    tiles=(
        Tile(
            title="Sun Peaks Events",
            url_name="sunpeaks_events",
            icon="sunpeaks_events/hub/icon.svg",
            section="mountain",
            subtitle="Resort events calendar feed",
            order=60,
        ),
    ),
    # The events calendar (an .ics with WebP pictures) other apps can read
    # once it is shared with them in Settings.
    shares=SHARES,
    api_version=2,
    # No hub data needed.
    permissions=(),
    # Scrapes sunpeaksresort.com.
    network=NETWORK_OUTBOUND,
    # Whatever wakes the app (a visit, or the hub's three-hourly tile refresh)
    # starts a background refresh when the feed is stale.
    lifecycle=LIFECYCLE_ON_DEMAND,
    wake_every=3 * 3600,
    memory_mb=160,
)
