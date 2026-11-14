"""Passkey storage and the WebAuthn ceremony helpers.

Same design as financesite's webauthn.py (read its header for the reasoning):

* The relying party is whichever host served the request -- the admin
  hostname behind Caddy, or localhost in development -- so there is no RP_ID
  setting to fall out of sync.
* The ceremony challenge travels in a short-lived signed cookie.
* A stable user handle is derived, not stored: there is exactly one admin.

Credentials live in a small JSON file on the admin container's private volume
(ADMIN_DATA_DIR/passkeys.json), written atomically. A handful of passkeys for
one person does not need a database. Only public keys are stored, so the file
is useless to anyone who reads it; losing it is what the password switch is for.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import Response

from .config import config

CHALLENGE_COOKIE = "bh_admin_challenge"
CHALLENGE_TTL_SECONDS = 5 * 60
RP_NAME = "Bird Heaven admin"
USER_NAME = "admin"
USER_DISPLAY_NAME = "Bird Heaven admin"

_lock = threading.Lock()


class CeremonyError(Exception):
    pass


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- store

def _read() -> list[dict[str, Any]]:
    path = config().credentials_file
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _write(creds: list[dict[str, Any]]) -> None:
    path = config().credentials_file
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".passkeys-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2)
    os.replace(tmp, path)


def all_credentials() -> list[dict[str, Any]]:
    with _lock:
        return _read()


def public_view(c: dict[str, Any]) -> dict[str, Any]:
    return {k: c.get(k) for k in ("id", "nickname", "created_at", "last_used_at")}


def add(credential_id: bytes, public_key: bytes, sign_count: int,
        transports: list[str] | None, nickname: str) -> dict[str, Any]:
    with _lock:
        creds = _read()
        entry = {
            "id": max((c["id"] for c in creds), default=0) + 1,
            "credential_id": b64(credential_id),
            "public_key": b64(public_key),
            "sign_count": sign_count,
            "transports": transports or [],
            "nickname": nickname[:60] or "Passkey",
            "created_at": _now(),
            "last_used_at": None,
        }
        creds.append(entry)
        _write(creds)
        return entry


def find_by_credential_id(raw: bytes) -> dict[str, Any] | None:
    key = b64(raw)
    return next((c for c in all_credentials() if c["credential_id"] == key), None)


def mark_used(entry_id: int, sign_count: int) -> None:
    with _lock:
        creds = _read()
        for c in creds:
            if c["id"] == entry_id:
                c["sign_count"] = sign_count
                c["last_used_at"] = _now()
        _write(creds)


def remove(entry_id: int) -> bool:
    with _lock:
        creds = _read()
        kept = [c for c in creds if c["id"] != entry_id]
        if len(kept) == len(creds):
            return False
        _write(kept)
        return True


def rename(entry_id: int, nickname: str) -> bool:
    with _lock:
        creds = _read()
        hit = False
        for c in creds:
            if c["id"] == entry_id:
                c["nickname"] = nickname[:60] or "Passkey"
                hit = True
        if hit:
            _write(creds)
        return hit


# ---------------------------------------------------------------- ceremony helpers

def rp_id(request: Request) -> str:
    return request.url.hostname or "localhost"


def expected_origin(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def user_handle() -> bytes:
    return hashlib.sha256(f"birdheaven-admin:{config().session_key}".encode()).digest()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config().session_key, salt="birdheaven-admin-webauthn")


def store_challenge(response: Response, challenge: bytes, purpose: str) -> None:
    token = _serializer().dumps({"c": challenge.hex(), "p": purpose, "iat": int(time.time())})
    response.set_cookie(CHALLENGE_COOKIE, token, max_age=CHALLENGE_TTL_SECONDS,
                        httponly=True, secure=config().cookie_secure, samesite="strict",
                        path="/api/webauthn")


def read_challenge(request: Request, purpose: str) -> bytes:
    token = request.cookies.get(CHALLENGE_COOKIE)
    if not token:
        raise CeremonyError("That took too long, or was started in another tab. Try again.")
    try:
        data = _serializer().loads(token, max_age=CHALLENGE_TTL_SECONDS)
    except (SignatureExpired, BadSignature) as err:
        raise CeremonyError("That took too long, or was started in another tab. Try again.") from err
    if data.get("p") != purpose:
        raise CeremonyError("That does not match what was started. Try again.")
    return bytes.fromhex(data["c"])


def clear_challenge(response: Response) -> None:
    response.delete_cookie(CHALLENGE_COOKIE, path="/api/webauthn")
