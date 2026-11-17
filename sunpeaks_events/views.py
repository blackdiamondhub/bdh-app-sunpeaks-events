import datetime
from zoneinfo import ZoneInfo

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from bdh_sdk import is_admin

from . import refresh

LOCAL = ZoneInfo("America/Vancouver")


def _local(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value).astimezone(LOCAL)
    except ValueError:
        return None


def _context():
    st = refresh.state()
    return {
        "last_refresh": _local(st.get("last_refresh")),
        "last_attempt": _local(st.get("last_attempt")),
        "events": st.get("events") or 0,
        "entries": st.get("entries") or 0,
        "images": st.get("images") or 0,
        "revision": st.get("revision"),
        "last_error": st.get("last_error") or "",
        "running": refresh.is_running(),
    }


def index(request):
    refresh.maybe_refresh_async()
    context = _context()
    context["admin"] = is_admin(request)
    return render(request, "sunpeaks_events/index.html", context)


def state(request):
    context = _context()
    for key in ("last_refresh", "last_attempt"):
        context[key] = context[key].isoformat() if context[key] else None
    return JsonResponse(context)


@require_POST
def refresh_now(request):
    if not is_admin(request):
        return JsonResponse({"error": "Only an admin can refresh the feed."}, status=403)
    return JsonResponse({"started": refresh.refresh_async(), "running": True})
