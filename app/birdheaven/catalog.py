"""The recordings catalog: one JSON document in the Spaces bucket.

Fetched server-side and cached briefly, so the browser never needs CORS access to
the bucket and a malformed catalog degrades to "fewer videos" instead of a broken
page. Entries that fail validation are skipped and logged, never served.

See ARCHITECTURE.md for the document's shape.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from .config import Config

log = logging.getLogger(__name__)

_MAX_BYTES = 5 * 1024 * 1024  # a catalog this big means something has gone wrong


class CatalogCache:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._videos: list[dict[str, Any]] = []
        self._updated: str | None = None
        self._fetched_at = 0.0
        self._ok = False

    # -- loading -----------------------------------------------------------

    def _read_raw(self) -> bytes:
        src = self._cfg.catalog_source
        if src.startswith(("http://", "https://")):
            req = urllib.request.Request(src, headers={"User-Agent": "birdheaven/1"})
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 -- configured URL
                return resp.read(_MAX_BYTES + 1)
        return Path(src).read_bytes()

    def _absolute(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        base = self._cfg.media_base_url
        return f"{base}/{path.lstrip('/')}" if base else f"/media/{path.lstrip('/')}"

    def _clean(self, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        slug, title, video = raw.get("slug"), raw.get("title"), raw.get("video")
        if not (isinstance(slug, str) and isinstance(title, str) and isinstance(video, str)):
            return None
        species = raw.get("species") or []
        if not isinstance(species, list):
            species = []
        poster = raw.get("poster")
        duration = raw.get("duration")
        return {
            "slug": slug,
            "title": title,
            "description": str(raw.get("description") or ""),
            "species": [str(s) for s in species if isinstance(s, str)][:10],
            "recorded": str(raw.get("recorded") or ""),
            "duration": float(duration) if isinstance(duration, (int, float)) else None,
            "video": self._absolute(video),
            "poster": self._absolute(poster) if isinstance(poster, str) and poster else None,
            "width": raw.get("width") if isinstance(raw.get("width"), int) else None,
            "height": raw.get("height") if isinstance(raw.get("height"), int) else None,
            "featured": bool(raw.get("featured", False)),
        }

    def _refresh(self) -> None:
        try:
            data = self._read_raw()
            if len(data) > _MAX_BYTES:
                raise ValueError("catalog exceeds size limit")
            doc = json.loads(data)
            entries = doc.get("videos", []) if isinstance(doc, dict) else []
            videos = []
            for raw in entries:
                clean = self._clean(raw)
                if clean is None:
                    log.warning("skipping malformed catalog entry: %.200r", raw)
                    continue
                videos.append(clean)
            videos.sort(key=lambda v: v["recorded"], reverse=True)
            self._videos = videos
            self._updated = doc.get("updated") if isinstance(doc, dict) else None
            self._ok = True
        except Exception as err:  # noqa: BLE001 -- keep serving the last good copy
            log.warning("catalog refresh failed (%s); serving %d cached entries",
                        err, len(self._videos))
            self._ok = bool(self._videos)
        finally:
            self._fetched_at = time.monotonic()

    def get(self) -> tuple[list[dict[str, Any]], str | None]:
        with self._lock:
            if time.monotonic() - self._fetched_at > self._cfg.catalog_ttl_seconds \
                    or self._fetched_at == 0.0:
                self._refresh()
            return self._videos, self._updated

    @property
    def healthy(self) -> bool:
        return self._ok
