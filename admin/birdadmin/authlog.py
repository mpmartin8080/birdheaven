"""The admin console's own sign-in log: who signed in, who failed, and when.

Caddy's access log (accesslog.py) records requests; this records what they
meant: a passkey or password sign-in, a failure and why, a throttled attempt,
a sign-out, and every passkey added, renamed or removed. Shown on the Logs
tab under "Sign-ins".

One JSON object per line in ADMIN_DATA_DIR/auth-log.jsonl, on the admin
container's private volume, so it survives deploys. At 1 MiB the file is
moved to auth-log.1.jsonl (replacing the previous one): together several
thousand events, bounded even if bots hammer the sign-in page.

Recording never raises. A full disk must not stop the admin from signing in.
Passwords are never logged, not even wrong ones.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.requests import Request

log = logging.getLogger(__name__)

FILE_NAME = "auth-log.jsonl"
ROLLED_NAME = "auth-log.1.jsonl"
MAX_BYTES = 1024 * 1024
MAX_LIMIT = 1000

# event -> succeeded? (None = neither: housekeeping like a sign-out)
EVENTS: dict[str, bool | None] = {
    "signin": True,              # detail: passkey nickname, or "recovery password"
    "signin_failed": False,      # detail: why
    "signin_throttled": False,   # too many recent failures; not even checked
    "password_disabled": False,  # password sign-in tried while the switch is off
    "signout": None,
    "passkey_added": True,
    "passkey_add_failed": False,
    "passkey_renamed": None,
    "passkey_removed": None,
}

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(data_dir: Path, event: str, request: Request | None = None, *,
           method: str = "", detail: str = "") -> None:
    entry: dict[str, Any] = {
        "ts": _now(),
        "event": event,
        "ok": EVENTS.get(event),
        "method": method,
        "detail": " ".join(detail.split())[:300],
        "ip": (request.client.host if request and request.client else ""),
        "userAgent": (request.headers.get("user-agent", "")[:400] if request else ""),
    }
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    path = data_dir / FILE_NAME
    try:
        with _lock:
            data_dir.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.stat().st_size + len(line) > MAX_BYTES:
                os.replace(path, data_dir / ROLLED_NAME)
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        log.exception("could not write the sign-in log")
    # Also in the container log, for `deploy-droplet logs admin`.
    level = logging.WARNING if entry["ok"] is False else logging.INFO
    log.log(level, "auth %s %s %s %s", event, method, entry["ip"], entry["detail"])


def _entries(data_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in (ROLLED_NAME, FILE_NAME):  # oldest file first
        path = data_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                e = json.loads(raw)
            except ValueError:
                continue  # a torn last line after a crash
            if isinstance(e, dict) and "event" in e:
                out.append(e)
    return out


def read(data_dir: Path, *, limit: int = 200, query: str = "", outcome: str = "") -> dict[str, Any]:
    """Newest first, filtered, plus a summary of everything on file."""
    limit = max(1, min(int(limit or 200), MAX_LIMIT))
    everything = _entries(data_dir)
    q = query.strip().lower()

    def keep(e: dict[str, Any]) -> bool:
        if outcome == "ok" and e.get("ok") is not True:
            return False
        if outcome == "fail" and e.get("ok") is not False:
            return False
        if q:
            hay = " ".join(str(e.get(k, "")) for k in
                           ("event", "method", "detail", "ip", "userAgent")).lower()
            if q not in hay:
                return False
        return True

    matched = [e for e in reversed(everything) if keep(e)]
    signins = [e for e in everything if e.get("event") == "signin"]
    failures = [e for e in everything if e.get("ok") is False]
    return {
        "available": bool(everything),
        "file": str(data_dir / FILE_NAME),
        "entries": matched[:limit],
        "summary": {
            "events": len(everything),
            "signIns": len(signins),
            "failures": len(failures),
            "failureIps": len({e.get("ip") for e in failures if e.get("ip")}),
            "lastSignIn": signins[-1] if signins else None,
            "since": everything[0].get("ts") if everything else None,
        },
    }
