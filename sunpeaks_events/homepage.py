from bdh_sdk import INFO, WARN, Status

from . import refresh


def status():
    refresh.maybe_refresh_async()
    st = refresh.state()
    entries = st.get("entries") or 0
    if st.get("revision") and entries:
        text = f"{entries} upcoming event{'s' if entries != 1 else ''}"
        return Status(text, WARN if st.get("last_error") else INFO)
    if st.get("last_error"):
        return Status("Events feed unavailable", WARN)
    return Status("Loading events…")
