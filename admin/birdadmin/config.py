"""Admin console configuration, all from the environment.

Unlike the public site, this container holds secrets: the session signing
key, the Spaces credentials, and (only while it is switched on) the recovery
password hash. They arrive from deploy/.env.prod via deploy/compose_ops.py.

THE PASSWORD SWITCH
    ADMIN_PASSWORD_LOGIN=true   Shows a password field on the sign-in page,
                                checked against ADMIN_PASSWORD_HASH (Argon2id).
                                Use it once to register the first passkey, or
                                to get back in after losing every passkey.
    ADMIN_PASSWORD_LOGIN=false  (default) Passkeys only. The password endpoint
                                does not exist; the hash may stay in the file.

The service refuses to start with the switch on and no hash, or without a
session key -- a misconfigured admin console must fail closed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60  # 8 hours


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AdminConfig:
    session_key: str
    session_ttl_seconds: int
    cookie_secure: bool
    password_login: bool
    password_hash: str
    data_dir: Path
    static_dir: Path

    # Storage: Spaces when keys are present, else a local folder (dev).
    spaces_key: str
    spaces_secret: str
    spaces_bucket: str
    spaces_region: str
    media_base_url: str
    local_storage_dir: str

    public_site_url: str
    transcode_preset: str
    max_upload_bytes: int

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def credentials_file(self) -> Path:
        return self.data_dir / "passkeys.json"

    def problems(self) -> list[str]:
        out = []
        if len(self.session_key) < 32:
            out.append("ADMIN_SESSION_KEY is missing or shorter than 32 characters.")
        if self.password_login and not self.password_hash.startswith("$argon2"):
            out.append("ADMIN_PASSWORD_LOGIN is on but ADMIN_PASSWORD_HASH is not an "
                       "Argon2 hash (generate one with `deploy-droplet admin-password`).")
        if not self.spaces_key and not self.local_storage_dir:
            out.append("No storage configured: set SPACES_KEY/SPACES_SECRET, or "
                       "ADMIN_LOCAL_STORAGE_DIR for local development.")
        return out


@lru_cache(maxsize=1)
def config() -> AdminConfig:
    here = Path(__file__).resolve().parent
    return AdminConfig(
        session_key=_str("ADMIN_SESSION_KEY"),
        session_ttl_seconds=int(_str("ADMIN_SESSION_TTL") or DEFAULT_SESSION_TTL_SECONDS),
        cookie_secure=_flag("ADMIN_COOKIE_SECURE", True),
        password_login=_flag("ADMIN_PASSWORD_LOGIN", False),
        password_hash=_str("ADMIN_PASSWORD_HASH"),
        data_dir=Path(_str("ADMIN_DATA_DIR") or (here.parents[1] / ".admin-data")),
        static_dir=Path(_str("ADMIN_STATIC_DIR") or (here.parent / "static")),
        spaces_key=_str("SPACES_KEY"),
        spaces_secret=_str("SPACES_SECRET"),
        spaces_bucket=_str("SPACES_BUCKET", "birdheaven"),
        spaces_region=_str("SPACES_REGION", "nyc3"),
        media_base_url=_str("MEDIA_BASE_URL"),
        local_storage_dir=_str("ADMIN_LOCAL_STORAGE_DIR"),
        public_site_url=_str("PUBLIC_SITE_URL", "/"),
        transcode_preset=_str("ADMIN_TRANSCODE_PRESET", "veryfast"),
        max_upload_bytes=int(_str("ADMIN_MAX_UPLOAD_MB") or 4096) * 1024 * 1024,
    )
