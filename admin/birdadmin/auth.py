"""Authentication for the one admin: sessions, the recovery password, throttling.

Follows financesite's auth.py, with two differences that matter here:

* **Passkeys are the normal way in; the password is a switch.** With
  ADMIN_PASSWORD_LOGIN off (the default) there is no password endpoint at all.
  Turn it on to register the first passkey or to recover, then turn it off.
* **A password session is marked as one.** The console shows a standing
  warning while you are signed in by password, so the switch does not get
  forgotten in the "on" position.

Sessions are a signed, timed cookie (itsdangerous), so there is no session
table; rotating ADMIN_SESSION_KEY signs everyone out. Cookies are SameSite=
Strict and every state-changing /api request must carry a matching Origin
header -- together, CSRF protection without tokens.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import Response

from .config import config

log = logging.getLogger(__name__)

COOKIE_NAME = "bh_admin"
_hasher = PasswordHasher()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config().session_key, salt="birdheaven-admin-session")


# ---------------------------------------------------------------- password

def password_login_enabled() -> bool:
    return config().password_login


def verify_password(candidate: str) -> bool:
    cfg = config()
    if not (cfg.password_login and cfg.password_hash and candidate):
        return False
    try:
        return _hasher.verify(cfg.password_hash, candidate)
    except Argon2Error:
        return False


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


# ---------------------------------------------------------------- throttling

_failures: list[float] = []
_WINDOW_SECONDS = 15 * 60
_MAX_FAILURES = 10


def _recent_failures() -> int:
    cutoff = time.monotonic() - _WINDOW_SECONDS
    _failures[:] = [t for t in _failures if t > cutoff]
    return len(_failures)


def throttled() -> bool:
    return _recent_failures() >= _MAX_FAILURES


def record_failure() -> None:
    _failures.append(time.monotonic())
    log.warning("failed admin sign-in (%d in the last 15 minutes)", _recent_failures())


def clear_failures() -> None:
    _failures.clear()


# ---------------------------------------------------------------- sessions

def issue_cookie(response: Response, method: str) -> None:
    cfg = config()
    token = _serializer().dumps({"iat": int(time.time()), "m": method})
    response.set_cookie(COOKIE_NAME, token, max_age=cfg.session_ttl_seconds,
                        httponly=True, secure=cfg.cookie_secure, samesite="strict", path="/")


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def session(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=config().session_ttl_seconds)
    except (SignatureExpired, BadSignature):
        return None
    if data.get("m") == "password" and not password_login_enabled():
        # Switching password login off also ends every password session.
        return None
    return data


def expected_origin(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def origin_ok(request: Request) -> bool:
    """True when a state-changing request came from this console's own pages."""
    origin = request.headers.get("origin")
    return bool(origin) and origin.rstrip("/") == expected_origin(request)
