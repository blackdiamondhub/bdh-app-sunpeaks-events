"""Event pictures: download, shrink to WebP, cache on disk.

Each picture is fetched with a conditional GET against the cached ETag /
Last-Modified, so unchanged pictures are neither re-downloaded nor
re-encoded. Pictures are handled one at a time to keep memory bounded.
"""
import io
import json
import re
from pathlib import Path

from PIL import Image

MAX_EDGE = 1600
QUALITY = 80
MAX_DOWNLOAD = 15 * 1024 * 1024
MIME = "image/webp"
_KEY_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def cache_dir():
    from django.conf import settings

    root = getattr(settings, "DATA_DIR", None) or settings.MEDIA_ROOT
    return Path(root) / "sunpeaks_events" / "images"


def attachment_key(slug):
    """``<slug>.webp``, fitted to the share attachment key rules."""
    stem = _KEY_RE.sub("-", slug).strip("-._")[:58] or "event"
    return f"{stem}.webp"


def to_webp(data, max_edge=MAX_EDGE, quality=QUALITY):
    """Re-encode image bytes as WebP no larger than ``max_edge`` on the long
    edge, without EXIF/ICC metadata."""
    with Image.open(io.BytesIO(data)) as img:
        img.draft("RGB", (max_edge, max_edge))  # cheap JPEG downscale while decoding
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)
        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        img = img.convert("RGBA" if has_alpha else "RGB")
        img.info.clear()  # no EXIF/ICC/XMP carried into the output
        out = io.BytesIO()
        img.save(out, "WEBP", quality=quality, method=4, exif=b"")
        img.close()
    return out.getvalue()


def fetch_image(fetcher, slug, url, directory=None):
    """The event's picture as WebP bytes, from cache when the source is
    unchanged. Returns None if there is no usable picture."""
    if not url:
        return None
    directory = Path(directory or cache_dir())
    directory.mkdir(parents=True, exist_ok=True)
    key = attachment_key(slug)
    path, meta_path = directory / key, directory / f"{key}.json"
    meta = {}
    if path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except ValueError:
            meta = {}
    headers = {}
    if meta.get("url") == url:
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]
    resp = fetcher.get(url, headers=headers)
    if resp.status_code == 304 and headers:
        return path.read_bytes()
    if resp.status_code != 200:
        return None
    data = resp.content
    if not data or len(data) > MAX_DOWNLOAD:
        return None
    webp = to_webp(data)
    del data
    path.write_bytes(webp)
    meta_path.write_text(json.dumps({
        "url": url,
        "etag": resp.headers.get("ETag", ""),
        "last_modified": resp.headers.get("Last-Modified", ""),
    }))
    return webp


def prune(directory, keep_keys):
    """Delete cached pictures of events no longer listed."""
    directory = Path(directory or cache_dir())
    if not directory.is_dir():
        return
    keep = set(keep_keys) | {f"{k}.json" for k in keep_keys}
    for child in directory.iterdir():
        if child.is_file() and child.name not in keep:
            child.unlink(missing_ok=True)
