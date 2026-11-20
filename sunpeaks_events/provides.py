"""Season hint for the hub: when the resort says winter opens."""
import datetime

from bdh_sdk.provides import WINTER, SeasonHint

from . import refresh


def build_season(raw):
    try:
        day = datetime.date.fromisoformat(raw or "")
    except ValueError:
        return None
    return SeasonHint(WINTER, "Sun Peaks opening day", opens_on=day)


def season():
    """The hub decides whether the date is current; stale dates are ignored there."""
    return build_season(refresh.state().get("opens_on"))
