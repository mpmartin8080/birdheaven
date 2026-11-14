"""Site settings: environment defaults, overlaid by site-settings.json.

The admin console writes `site-settings.json` into the bucket beside
`catalog.json`. This module reads it the same way catalog.py reads the catalog
-- server-side, cached, validated -- and merges it over the env defaults from
config.py. Anything missing or invalid in the file falls back to the default,
so a bad edit can never take the page down.

Shape (every key optional):

    {"siteName": "...", "tagline": "...", "intro": "...", "location": "...",
     "paypalHostedButtonId": "...",
     "live": {"status": "offline|live", "kind": "hls|video|iframe",
              "url": "https://...", "title": "..."}}
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import Config

log = logging.getLogger(__name__)

LIVE_KINDS = {"hls", "video", "iframe"}
_PAYPAL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_BYTES = 64 * 1024


def _text(value: Any, limit: int) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) else None


def _url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v == "" or v.startswith(("https://", "http://", "/")):
        return v[:2000]
    return None


class SettingsCache:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._overrides: dict[str, Any] = {}
        self._fetched_at = 0.0

    def _read(self) -> dict[str, Any]:
        src = self._cfg.settings_source
        if src.startswith(("http://", "https://")):
            req = urllib.request.Request(src, headers={"User-Agent": "birdheaven/1"})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                    data = resp.read(_MAX_BYTES + 1)
            except urllib.error.HTTPError as err:
                if err.code in (403, 404):   # not written yet: Spaces says 403 or 404
                    return {}
                raise
        else:
            path = Path(src)
            if not path.is_file():
                return {}
            data = path.read_bytes()
        if len(data) > _MAX_BYTES:
            raise ValueError("settings file too large")
        doc = json.loads(data)
        return doc if isinstance(doc, dict) else {}

    def _refresh(self) -> None:
        try:
            self._overrides = self._read()
        except Exception as err:  # noqa: BLE001 -- keep the last good overrides
            log.warning("site settings refresh failed (%s); keeping previous values", err)
        finally:
            self._fetched_at = time.monotonic()

    def get(self) -> dict[str, Any]:
        with self._lock:
            if self._fetched_at == 0.0 or \
                    time.monotonic() - self._fetched_at > self._cfg.catalog_ttl_seconds:
                self._refresh()
            o = self._overrides
        cfg = self._cfg
        live_o = o.get("live") if isinstance(o.get("live"), dict) else {}

        paypal = _text(o.get("paypalHostedButtonId"), 64)
        if paypal is not None and paypal and not _PAYPAL_ID.match(paypal):
            paypal = None
        status = _text(live_o.get("status"), 16)
        kind = _text(live_o.get("kind"), 16)

        def pick(value, default):
            return default if value is None else value

        return {
            "siteName": pick(_text(o.get("siteName"), 80), cfg.site_name) or cfg.site_name,
            "tagline": pick(_text(o.get("tagline"), 200), cfg.site_tagline),
            # The hero paragraph. Empty means "keep the text in index.html".
            "intro": _text(o.get("intro"), 600) or "",
            "location": pick(_text(o.get("location"), 80), cfg.location_label),
            "paypalHostedButtonId": pick(paypal, cfg.paypal_hosted_button_id),
            "live": {
                "status": pick(status.lower() if status else status, cfg.live_status),
                "kind": pick(kind.lower() if kind else kind, cfg.live_kind),
                "url": pick(_url(live_o.get("url")), cfg.live_url),
                "title": pick(_text(live_o.get("title"), 80), cfg.live_title) or "Feeder cam",
            },
        }
