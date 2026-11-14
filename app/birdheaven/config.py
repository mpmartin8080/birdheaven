"""Configuration, all of it from the environment.

Nothing here is a secret. The site is public and read-only: the container needs
to know where the catalog and media live, and the *defaults* for the site text,
PayPal button and live stream. Those defaults are overridden at runtime by
`site-settings.json`, which the admin console writes next to `catalog.json` in
the bucket (see sitesettings.py) -- so most day-to-day changes need no deploy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[2]

SETTINGS_OBJECT = "site-settings.json"


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def origin_of(url: str) -> str:
    """scheme://host[:port] of an absolute http(s) URL, or "" otherwise."""
    parts = urlsplit(url or "")
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return ""


def _sibling(source: str, name: str) -> str:
    """`name` in the same directory (or URL prefix) as `source`."""
    if source.startswith(("http://", "https://")):
        return source.rsplit("/", 1)[0] + "/" + name
    return str(Path(source).with_name(name))


@dataclass(frozen=True)
class Config:
    static_dir: Path

    # Where catalog.json and site-settings.json are read from: http(s) URLs in
    # production (the Spaces bucket), filesystem paths in local development.
    catalog_source: str
    settings_source: str
    catalog_ttl_seconds: int

    # Prefix for relative `video`/`poster` paths in the catalog.
    media_base_url: str

    # Defaults; site-settings.json overrides each one it sets.
    site_name: str
    site_tagline: str
    location_label: str
    paypal_hosted_button_id: str
    live_status: str
    live_kind: str
    live_url: str
    live_title: str

    @property
    def media_origin(self) -> str:
        return origin_of(self.media_base_url)


@lru_cache(maxsize=1)
def config() -> Config:
    catalog_source = _str("CATALOG_URL") or str(_REPO_ROOT / "dev" / "media" / "catalog.json")
    media_base = _str("MEDIA_BASE_URL")
    if not media_base and catalog_source.startswith(("http://", "https://")):
        media_base = catalog_source.rsplit("/", 1)[0]
    return Config(
        static_dir=Path(_str("STATIC_DIR") or (_REPO_ROOT / "webroot")),
        catalog_source=catalog_source,
        settings_source=_str("SETTINGS_URL") or _sibling(catalog_source, SETTINGS_OBJECT),
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
