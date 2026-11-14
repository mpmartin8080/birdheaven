"""Live video status -- a deliberate stub.

The streaming protocol has not been chosen yet (see ARCHITECTURE.md, "Live
video"). Until it is, the status is whatever the environment says:

    LIVE_STATUS   offline (default) | live
    LIVE_KIND     hls | video | iframe
    LIVE_URL      the playlist / file / embed URL
    LIVE_TITLE    shown above the player

The front end only ever reads the dict returned by `status()`, so replacing this
function with a real probe of an ingest server (MediaMTX's API, Owncast's
/api/status, ...) changes nothing on the page.
"""

from __future__ import annotations

from typing import Any

from .config import Config

KINDS = {"hls", "video", "iframe"}


def status(cfg: Config) -> dict[str, Any]:
    live = cfg.live_status == "live" and cfg.live_kind in KINDS and bool(cfg.live_url)
    return {
        "status": "live" if live else "offline",
        "kind": cfg.live_kind if live else None,
        "url": cfg.live_url if live else None,
        "title": cfg.live_title,
    }
