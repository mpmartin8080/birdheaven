"""The values in deploy/.env.prod that are handed to the containers.

Replaces financesite's secrets_cli.py. Two groups go to `docker compose`:

  * SITE_KEYS -- defaults for the public `web` service (catalog URL, site
    text, PayPal, live stub). Not secret. The admin console's Site settings
    page overrides most of them at runtime.
  * ADMIN_KEYS -- the `admin` service's secrets and switches: its session
    signing key, the recovery-password switch and hash, and the Spaces key it
    uploads with. Only the admin container receives these.

As in financesite, any value may be an `op://` 1Password reference, resolved
in memory at the moment it is needed. Values reach Compose through the
subprocess environment, never --env-file, so the `$`s in an Argon2 hash
survive intact (see compose_ops.py).
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

ADMIN_KEYS = (
    "ADMIN_SESSION_KEY",
    "ADMIN_PASSWORD_LOGIN",
    "ADMIN_PASSWORD_HASH",
    "SPACES_KEY",
    "SPACES_SECRET",
    "SPACES_BUCKET",
    "SPACES_REGION",
)

_SECRET = {"ADMIN_SESSION_KEY", "ADMIN_PASSWORD_HASH", "SPACES_KEY", "SPACES_SECRET"}


def compose_values(cfg: DeployConfig) -> dict[str, str]:
    raw = parse_env_file(cfg.env_file)
    picked = {k: raw[k] for k in (*SITE_KEYS, *ADMIN_KEYS) if raw.get(k)}
    return onepassword.resolve(picked)


def set_values(cfg: DeployConfig, updates: dict[str, str]) -> None:
    """Rewrite KEY=value lines in deploy/.env.prod, keeping every other line as-is."""
    path = cfg.env_file
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    if remaining:
        out += ["", "# Added by deploy-droplet admin-password"]
        out += [f"{k}={v}" for k, v in remaining.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def value(cfg: DeployConfig, key: str) -> str:
    raw = parse_env_file(cfg.env_file).get(key, "").strip()
    return onepassword.resolve({key: raw})[key] if raw else ""


def check(cfg: DeployConfig) -> None:
    """Report where each setting comes from without printing any secret."""
    raw = parse_env_file(cfg.env_file)
    if not raw:
        print(f"  {cfg.env_file} does not exist or is empty -- copy deploy/.env.example")
        return
    for key in (*SITE_KEYS, *ADMIN_KEYS):
        v = raw.get(key, "").strip()
        if not v:
            state = "(unset)"
        elif onepassword.is_reference(v):
            onepassword.resolve({key: v})
            state = f"1Password {v} -- resolves"
        elif key in _SECRET:
            state = "set in file"
        else:
            state = v
        print(f"  {key:28} {state}")
    if not raw.get("CATALOG_URL"):
        print("\n  warning: CATALOG_URL unset -- the deployed site will show no recordings.")
    if not raw.get("ADMIN_SESSION_KEY"):
        print("  warning: ADMIN_SESSION_KEY unset -- the admin console will not start. "
              "Run `deploy-droplet admin-password`.")
    if raw.get("ADMIN_PASSWORD_LOGIN", "").strip().lower() in {"1", "true", "yes", "on"}:
        print("  note: ADMIN_PASSWORD_LOGIN is ON. Switch it off once a passkey is registered.")
