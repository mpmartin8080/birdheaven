"""Clip processing and catalog/settings editing. Shared by the admin console
and tools/publish.py, so the command line and the GUI publish identically.

Processing a clip:

  1. ffprobe the upload.
  2. H.264 already -> remux only (`-c copy`, seconds even for big files).
     Anything else -> transcode to H.264/AAC, capped at 1080p. `veryfast` by
     default: the admin console runs on a small droplet, and for feeder-cam
     footage the size difference vs `slow` is modest.
  3. `+faststart` either way, so browsers can play before downloading it all.
  4. Poster frame as JPEG (a third of the way in unless told otherwise).
  5. Upload both, then add the entry to catalog.json.

Media objects are immutable (one-year cache), so a regenerated poster gets a
new key and the old one is deleted.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .storage import Storage

CATALOG_KEY = "catalog.json"
SETTINGS_KEY = "site-settings.json"

# One writer at a time for catalog.json / site-settings.json within a process.
_doc_lock = threading.RLock()


class MediaError(Exception):
    """A problem worth showing to the person who caused it, as-is."""


# ---------------------------------------------------------------- ffmpeg

def _need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise MediaError(f"`{tool}` is not installed (FFmpeg is required to process video).")
    return path


def _run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-4:])
        raise MediaError(f"{Path(args[0]).name} failed: {tail or 'exit ' + str(result.returncode)}")
    return result.stdout


def probe(src: str | Path) -> dict[str, Any]:
    try:
        out = _run([_need("ffprobe"), "-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams", str(src)])
    except MediaError as err:
        if "not installed" in str(err):
            raise
        raise MediaError("That file isn't a video FFmpeg can read. Try exporting it "
                         "as MP4 (H.264) first.") from err
    info = json.loads(out)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise MediaError("That file has no video stream.")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = info.get("format", {})
    tags = fmt.get("tags", {}) or {}
    return {
        "duration": float(fmt.get("duration") or 0) or None,
        "width": video.get("width"),
        "height": video.get("height"),
        "vcodec": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "acodec": audio.get("codec_name") if audio else None,
        "creation_time": tags.get("creation_time"),
    }


def can_remux(info: dict[str, Any]) -> bool:
    """True when the streams are already browser-safe and only the container needs fixing."""
    return (info["vcodec"] == "h264" and info.get("pix_fmt") in (None, "yuv420p", "yuvj420p")
            and (info["height"] or 0) <= 1080 and info["acodec"] in (None, "aac"))


def encode(src: Path, dst: Path, *, remux: bool, crf: int = 23, preset: str = "veryfast") -> None:
    if remux:
        codec = ["-c", "copy"]
    else:
        codec = [
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-vf", "scale='min(1920,iw)':'-2'",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        ]
    _run([_need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
          "-i", str(src), *codec, "-movflags", "+faststart", str(dst)])


def poster(src: str | Path, dst: Path, at: float) -> None:
    _run([_need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
          "-ss", f"{max(at, 0):.2f}", "-i", str(src), "-frames:v", "1",
          "-vf", "scale='min(1280,iw)':'-2'", "-q:v", "3", str(dst)])


# ---------------------------------------------------------------- catalog

def load_catalog(storage: Storage) -> dict[str, Any]:
    doc = storage.get_json(CATALOG_KEY) or {}
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("version", 1)
    if not isinstance(doc.get("videos"), list):
        doc["videos"] = []
    return doc


def save_catalog(storage: Storage, doc: dict[str, Any]) -> None:
    doc["updated"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    doc["videos"].sort(key=lambda v: v.get("recorded", ""), reverse=True)
    storage.put_json(CATALOG_KEY, doc)


def edit_catalog(storage: Storage, fn: Callable[[dict[str, Any]], Any]) -> Any:
    """Read-modify-write catalog.json under the process lock; returns fn's result."""
    with _doc_lock:
        doc = load_catalog(storage)
        result = fn(doc)
        save_catalog(storage, doc)
        return result


def find(doc: dict[str, Any], slug: str) -> dict[str, Any]:
    entry = next((v for v in doc["videos"] if v.get("slug") == slug), None)
    if entry is None:
        raise MediaError(f"No clip called {slug}.")
    return entry


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "clip"


def unique_slug(doc: dict[str, Any], base: str) -> str:
    taken = {v.get("slug") for v in doc["videos"]}
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def clean_species(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for s in raw:
        if isinstance(s, str) and (s := " ".join(s.split())[:60]) and s not in out:
            out.append(s)
    return out[:10]


EDITABLE = ("title", "description", "species", "recorded", "featured")


def apply_edits(entry: dict[str, Any], fields: dict[str, Any]) -> None:
    if "title" in fields:
        title = " ".join(str(fields["title"] or "").split())[:120]
        if not title:
            raise MediaError("A clip needs a title.")
        entry["title"] = title
    if "description" in fields:
        entry["description"] = str(fields["description"] or "").strip()[:1000]
    if "species" in fields:
        entry["species"] = clean_species(fields["species"])
    if "recorded" in fields and fields["recorded"]:
        entry["recorded"] = parse_when(str(fields["recorded"])).isoformat()
    if "featured" in fields:
        entry["featured"] = bool(fields["featured"])


def parse_when(value: str) -> datetime:
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as err:
        raise MediaError(f"Not a date and time: {value!r}") from err
    if when.tzinfo is None:
        when = when.astimezone()
    return when.replace(microsecond=0)


# ---------------------------------------------------------------- publishing

@dataclass
class ClipMeta:
    title: str
    description: str = ""
    species: list[str] = field(default_factory=list)
    recorded: datetime | None = None
    featured: bool = False
    poster_at: float | None = None
    slug_hint: str | None = None


def publish_clip(storage: Storage, src: Path, meta: ClipMeta, workdir: Path, *,
                 force_transcode: bool = False, crf: int = 23, preset: str = "veryfast",
                 progress: Callable[[str], None] = lambda _msg: None) -> dict[str, Any]:
    """Process `src` and publish it. Returns the new catalog entry."""
    progress("Inspecting the file")
    info = probe(src)

    recorded = meta.recorded
    if recorded is None and info["creation_time"]:
        try:
            recorded = parse_when(info["creation_time"])
        except MediaError:
            recorded = None
    if recorded is None:
        recorded = datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc).replace(microsecond=0)

    base = f"{recorded:%Y-%m-%d}-{slugify(meta.slug_hint or meta.title)}"
    with _doc_lock:
        slug = unique_slug(load_catalog(storage), base)
    prefix = f"{recorded:%Y/%m}"
    video_key = f"videos/{prefix}/{slug}.mp4"
    poster_key = f"posters/{prefix}/{slug}.jpg"

    workdir.mkdir(parents=True, exist_ok=True)
    out_mp4 = workdir / f"{slug}.mp4"
    out_jpg = workdir / f"{slug}.jpg"
    remux = can_remux(info) and not force_transcode
    progress("Optimising for streaming" if remux else "Converting to H.264 (this can take a while)")
    encode(src, out_mp4, remux=remux, crf=crf, preset=preset)
    final = probe(out_mp4)

    progress("Grabbing a poster frame")
    at = meta.poster_at if meta.poster_at is not None else (final["duration"] or 3) / 3
    poster(out_mp4, out_jpg, min(at, max((final["duration"] or 1) - 0.1, 0)))

    progress("Uploading video")
    storage.put_file(video_key, out_mp4, "video/mp4")
    progress("Uploading poster")
    storage.put_file(poster_key, out_jpg, "image/jpeg")

    entry = {
        "slug": slug,
        "title": meta.title,
        "description": meta.description,
        "species": clean_species(meta.species),
        "recorded": recorded.isoformat(),
        "duration": round(final["duration"] or 0, 1) or None,
        "video": video_key,
        "poster": poster_key,
        "width": final["width"],
        "height": final["height"],
        "bytes": out_mp4.stat().st_size,
        "featured": meta.featured,
    }
    progress("Adding to the catalog")

    def add(doc: dict[str, Any]) -> None:
        doc["videos"] = [v for v in doc["videos"] if v.get("slug") != slug] + [entry]
    edit_catalog(storage, add)
    out_mp4.unlink(missing_ok=True)
    out_jpg.unlink(missing_ok=True)
    return entry


def regenerate_poster(storage: Storage, slug: str, at: float, workdir: Path) -> dict[str, Any]:
    with _doc_lock:
        entry = dict(find(load_catalog(storage), slug))
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / f"{slug}-poster.jpg"
    poster(storage.read_url(entry["video"]), out, at)
    stem = entry["video"].rsplit("/", 1)[0].replace("videos/", "posters/", 1)
    new_key = f"{stem}/{slug}-{int(time.time())}.jpg"
    storage.put_file(new_key, out, "image/jpeg")
    out.unlink(missing_ok=True)

    old: dict[str, str | None] = {"key": None}

    def swap(doc: dict[str, Any]) -> dict[str, Any]:
        e = find(doc, slug)
        old["key"] = e.get("poster")
        e["poster"] = new_key
        return dict(e)
    updated = edit_catalog(storage, swap)
    if old["key"] and old["key"] != new_key:
        storage.delete(old["key"])
    return updated


def remove_clip(storage: Storage, slug: str, *, delete_files: bool) -> dict[str, Any]:
    def drop(doc: dict[str, Any]) -> dict[str, Any]:
        e = find(doc, slug)
        doc["videos"].remove(e)
        return e
    entry = edit_catalog(storage, drop)
    if delete_files:
        for key in (entry.get("video"), entry.get("poster")):
            if key:
                storage.delete(key)
    return entry


# ---------------------------------------------------------------- site settings

LIVE_KINDS = ("", "hls", "video", "iframe")
_PAYPAL_ID = re.compile(r"^[A-Za-z0-9_-]{0,64}$")


def load_settings(storage: Storage) -> dict[str, Any]:
    doc = storage.get_json(SETTINGS_KEY)
    return doc if isinstance(doc, dict) else {}


def clean_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate what the settings form sent. Raises MediaError with a readable reason."""
    def text(key: str, limit: int, src: dict[str, Any] = raw) -> str:
        return " ".join(str(src.get(key) or "").split())[:limit]

    live_raw = raw.get("live") if isinstance(raw.get("live"), dict) else {}
    paypal = text("paypalHostedButtonId", 64)
    if not _PAYPAL_ID.match(paypal):
        raise MediaError("The PayPal button ID should be letters and digits only "
                         "(the hosted_button_id value from PayPal).")
    status = text("status", 16, live_raw).lower() or "offline"
    if status not in ("offline", "live"):
        raise MediaError("Live status must be offline or live.")
    kind = text("kind", 16, live_raw).lower()
    if kind not in LIVE_KINDS:
        raise MediaError("Live stream type must be HLS, video or embed.")
    url = str(live_raw.get("url") or "").strip()[:2000]
    if url and not url.startswith(("https://", "/")):
        raise MediaError("The live stream URL must start with https:// (or / for a "
                         "path on this site).")
    if status == "live" and not (kind and url):
        raise MediaError("To go live, choose a stream type and enter its URL.")
    intro = str(raw.get("intro") or "").strip()[:600]
    name = text("siteName", 80)
    if not name:
        raise MediaError("The site needs a name.")
    return {
        "version": 1,
        "siteName": name,
        "tagline": text("tagline", 200),
        "intro": intro,
        "location": text("location", 80),
        "paypalHostedButtonId": paypal,
        "live": {"status": status, "kind": kind, "url": url,
                 "title": text("title", 80, live_raw) or "Feeder cam"},
    }


def save_settings(storage: Storage, settings: dict[str, Any]) -> dict[str, Any]:
    with _doc_lock:
        settings = dict(settings)
        settings["updated"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        storage.put_json(SETTINGS_KEY, settings)
    return settings
