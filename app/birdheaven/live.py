"""Live video status -- a deliberate stub.

The streaming protocol has not been chosen yet (see ARCHITECTURE.md, "Live
video"). Until it is, the status is whatever the site settings say -- set in
the admin console, or by the LIVE_* environment defaults:

    status   offline (default) | live
    kind     hls | video | iframe
    url      the playlist / file / embed URL
    title    shown under the player

The front end only ever reads the dict returned by `status()`, so replacing this
function with a real probe of an ingest server (MediaMTX's API, Owncast's
/api/status, ...) changes nothing on the page.
"""

from __future__ import annotations

from typing import Any

from .sitesettings import LIVE_KINDS


def status(settings: dict[str, Any]) -> dict[str, Any]:
    live = settings["live"]
    on = live["status"] == "live" and live["kind"] in LIVE_KINDS and bool(live["url"])
    return {
        "status": "live" if on else "offline",
        "kind": live["kind"] if on else None,
        "url": live["url"] if on else None,
        "title": live["title"],
    }
