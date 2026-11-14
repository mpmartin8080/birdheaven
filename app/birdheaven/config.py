"""Configuration, all of it from the environment.

Nothing here is a secret. The site is public and read-only: the container needs
to know where the media lives and which PayPal button to show, and that is all.
The Spaces credentials used to *publish* video live only on the owner's
workstation (see tools/publish.py) and never reach this process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _origin(url: str) -> str:
    """scheme://host[:port] of a URL, or "" if it is relative or empty."""
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return ""


@dataclass(frozen=True)
class Config:
    static_dir: Path

    # Where catalog.json is read from. An http(s) URL in production (the Spaces
    # CDN), or a filesystem path for local development.
    catalog_source: str
    catalog_ttl_seconds: int

    # Prefix for the relative `video`/`poster` paths inside the catalog, e.g.
    # https://birdheaven.nyc3.cdn.digitaloceanspaces.com. Defaults to the
    # directory the catalog itself came from.
    media_base_url: str

    site_name: str
    site_tagline: str
    location_label: str

    paypal_hosted_button_id: str

    # Live video stub -- see live.py.
    live_status: str
    live_kind: str
    live_url: str
    live_title: str

    @property
    def media_origin(self) -> str:
        return _origin(self.media_base_url)

    @property
    def live_origin(self) -> str:
        return _origin(self.live_url)


@lru_cache(maxsize=1)
def config() -> Config:
    catalog_source = _str("CATALOG_URL") or str(_REPO_ROOT / "dev" / "catalog.json")
    media_base = _str("MEDIA_BASE_URL")
    if not media_base and catalog_source.startswith(("http://", "https://")):
        media_base = catalog_source.rsplit("/", 1)[0]
    static_dir = Path(_str("STATIC_DIR") or (_REPO_ROOT / "webroot"))
    return Config(
        static_dir=static_dir,
        catalog_source=catalog_source,
        catalog_ttl_seconds=int(_str("CATALOG_TTL_SECONDS", "60") or 60),
        media_base_url=media_base.rstrip("/"),
        site_name=_str("SITE_NAME", "Bird Heaven"),
        site_tagline=_str("SITE_TAGLINE", "Live from the backyard feeders"),
        location_label=_str("LOCATION_LABEL", ""),
        paypal_hosted_button_id=_str("PAYPAL_HOSTED_BUTTON_ID"),
        live_status=_str("LIVE_STATUS", "offline").lower(),
        live_kind=_str("LIVE_KIND").lower(),
        live_url=_str("LIVE_URL"),
        live_title=_str("LIVE_TITLE", "Feeder cam"),
    )
