"""Configuration, all of it from the environment.

The site is public and read-only: the container needs to know where the catalog
and media live, and the *defaults* for the site text, PayPal button and live
stream. Its one secret is the SMTP password the contact form sends mail with
(it holds no storage credentials). The site-text defaults are overridden at runtime by
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

# Where the contact form sends messages unless CONTACT_EMAIL says otherwise.
DEFAULT_CONTACT_EMAIL = "bird.heaven.admin@gmail.com"


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

    # Contact form (contact.py). The only secret here is smtp_password.
    contact_email: str
    contact_from: str
    smtp_host: str
    smtp_port: int
    smtp_security: str  # starttls | ssl | none
    smtp_username: str
    smtp_password: str

    @property
    def media_origin(self) -> str:
        return origin_of(self.media_base_url)


@lru_cache(maxsize=1)
def config() -> Config:
    catalog_source = _str("CATALOG_URL") or str(_REPO_ROOT / "dev" / "media" / "catalog.json")
    media_base = _str("MEDIA_BASE_URL")
    if not media_base and catalog_source.startswith(("http://", "https://")):
        media_base = catalog_source.rsplit("/", 1)[0]
    security = _str("SMTP_SECURITY", "starttls").lower()
    if security not in ("starttls", "ssl", "none"):
        raise ValueError(f"SMTP_SECURITY must be starttls, ssl or none (got {security!r})")
    contact_email = _str("CONTACT_EMAIL") or DEFAULT_CONTACT_EMAIL
    smtp_username = _str("SMTP_USERNAME")
    return Config(
        contact_email=contact_email,
        # Many relays only accept a verified sender; default to the login,
        # which for Gmail-style providers is the mailbox itself.
        contact_from=_str("CONTACT_FROM") or (smtp_username if "@" in smtp_username else contact_email),
        smtp_host=_str("SMTP_HOST"),
        smtp_port=int(_str("SMTP_PORT") or (465 if security == "ssl" else 587)),
        smtp_security=security,
        smtp_username=smtp_username,
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
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
