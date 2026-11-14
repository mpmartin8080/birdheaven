"""The values in deploy/.env.prod that are handed to the containers.

Replaces financesite's secrets_cli.py. The running site has no secrets -- no
password, no session key, no database -- so there is nothing to generate. What
remains is *site settings* (media URL, PayPal button, live stream stub) that
compose.prod.yaml substitutes into the `web` service, plus the Spaces
credentials that only ever stay on this workstation:

    SPACES_KEY / SPACES_SECRET  uploading clips (tools/publish.py)

Those are never passed to `docker compose`. As in financesite, any value may be
an `op://` 1Password reference, resolved in memory at the moment it is needed.
"""

from __future__ import annotations

from . import onepassword
from .config import DeployConfig, parse_env_file

# Substituted into deploy/compose.prod.yaml. Everything else in the file stays
# on the workstation.
SITE_KEYS = (
    "CATALOG_URL",
    "MEDIA_BASE_URL",
    "SITE_NAME",
    "SITE_TAGLINE",
    "LOCATION_LABEL",
    "PAYPAL_HOSTED_BUTTON_ID",
    "LIVE_STATUS",
    "LIVE_KIND",
    "LIVE_URL",
    "LIVE_TITLE",
    "LOG_LEVEL",
)

_WORKSTATION_ONLY = ("SPACES_KEY", "SPACES_SECRET")


def compose_values(cfg: DeployConfig) -> dict[str, str]:
    raw = parse_env_file(cfg.env_file)
    picked = {k: raw[k] for k in SITE_KEYS if raw.get(k)}
    return onepassword.resolve(picked)


def value(cfg: DeployConfig, key: str) -> str:
    raw = parse_env_file(cfg.env_file).get(key, "").strip()
    return onepassword.resolve({key: raw})[key] if raw else ""


def check(cfg: DeployConfig) -> None:
    """Report where each setting comes from without printing any secret."""
    raw = parse_env_file(cfg.env_file)
    if not raw:
        print(f"  {cfg.env_file} does not exist or is empty -- copy deploy/.env.example")
        return
    for key in (*SITE_KEYS, *_WORKSTATION_ONLY):
        v = raw.get(key, "").strip()
        if not v:
            state = "(unset)"
        elif onepassword.is_reference(v):
            onepassword.resolve({key: v})
            state = f"1Password {v} -- resolves"
        elif key in _WORKSTATION_ONLY:
            state = "set in file"
        else:
            state = v
        print(f"  {key:28} {state}")
    if not raw.get("CATALOG_URL"):
        print("\n  warning: CATALOG_URL unset -- the deployed site will show no recordings.")
    if not raw.get("PAYPAL_HOSTED_BUTTON_ID"):
        print("  note: PAYPAL_HOSTED_BUTTON_ID unset -- the donate panel stays hidden.")
