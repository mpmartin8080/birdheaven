"""Read Caddy's HTTP access logs for the Logs tab.

Caddy terminates every request for both containers, so its access logs are
the complete record: one file for the public site (ports 80 and 443) and one
for this console (its own port). It writes them as JSON lines to the
`caddy-logs` volume (deploy/caddy/Caddyfile), which is mounted read-only into
this container at ADMIN_ACCESS_LOG_DIR (/logs). This container never gets the
Docker socket -- that would be root on the droplet.

Only the tail of each file is read (TAIL_BYTES), plus the newest rolled-over
file when the current one has just been rotated, so a request is cheap however
large the logs grow.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SOURCES = {
    "site": ("site-access.log", "Public site"),
    "admin": ("admin-access.log", "Admin console"),
}
TAIL_BYTES = 4 * 1024 * 1024
MAX_LIMIT = 1000


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    """The last complete lines within max_bytes of the end of the file."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # first line is probably cut in half
    return lines


def _rolled(directory: Path, name: str) -> Path | None:
    """The newest rotated copy of `name` (Caddy names them stem-<timestamp>.log)."""
    stem, ext = os.path.splitext(name)
    try:
        backups = sorted((p for p in directory.glob(f"{stem}-*{ext}") if p.is_file()),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return backups[0] if backups else None


def _first(headers: dict, name: str) -> str:
    v = headers.get(name) or []
    return str(v[0]) if isinstance(v, list) and v else str(v or "")


def parse(line: str) -> dict | None:
    """One Caddy JSON access-log line -> the fields the console shows."""
    try:
        e = json.loads(line)
    except ValueError:
        return None
    if not isinstance(e, dict) or not isinstance(e.get("request"), dict):
        return None
    req = e["request"]
    headers = req.get("headers") if isinstance(req.get("headers"), dict) else {}
    try:
        ts = datetime.fromtimestamp(float(e.get("ts")), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        ts = ""
    try:
        duration_ms = round(float(e.get("duration") or 0) * 1000, 1)
    except (TypeError, ValueError):
        duration_ms = None
    return {
        "ts": ts,
        "ip": str(req.get("client_ip") or req.get("remote_ip") or ""),
        "method": str(req.get("method") or ""),
        "host": str(req.get("host") or ""),
        "uri": str(req.get("uri") or ""),
        "proto": str(req.get("proto") or ""),
        "status": int(e.get("status") or 0) if str(e.get("status") or "0").isdigit() else 0,
        "size": int(e.get("size") or 0) if str(e.get("size") or "0").isdigit() else 0,
        "durationMs": duration_ms,
        "userAgent": _first(headers, "User-Agent")[:300],
        "referer": _first(headers, "Referer")[:300],
    }


def _matches(entry: dict, query: str, status: str, hide_api: bool) -> bool:
    if status and not str(entry["status"]).startswith(status[0]):
        return False
    if hide_api and entry["uri"].startswith("/api/"):
        return False
    if query:
        hay = " ".join((entry["ip"], entry["method"], entry["uri"], str(entry["status"]),
                        entry["userAgent"], entry["referer"])).lower()
        return query in hay
    return True


def read(log_dir: Path, source: str, *, limit: int = 200, query: str = "",
         status: str = "", hide_api: bool = False) -> dict:
    if source not in SOURCES:
        raise ValueError(f"unknown log {source!r}")
    name, label = SOURCES[source]
    limit = max(1, min(int(limit), MAX_LIMIT))
    query = query.strip().lower()[:200]
    status = status if status in ("2xx", "3xx", "4xx", "5xx") else ""
    path = log_dir / name

    result = {"source": source, "label": label, "file": str(path), "available": path.is_file(),
              "entries": [], "scanned": 0, "summary": {}}
    if not result["available"]:
        return result

    lines = _tail_lines(path, TAIL_BYTES)
    if len(lines) < limit:
        older = _rolled(log_dir, name)
        if older:
            lines = _tail_lines(older, TAIL_BYTES) + lines

    entries, scanned, ips, errors = [], 0, set(), 0
    for line in reversed(lines):  # newest first
        entry = parse(line)
        if entry is None:
            continue
        scanned += 1
        ips.add(entry["ip"])
        if entry["status"] >= 400:
            errors += 1
        if len(entries) < limit and _matches(entry, query, status, hide_api):
            entries.append(entry)
    result.update(entries=entries, scanned=scanned,
                  summary={"requests": scanned, "visitors": len(ips), "errors": errors,
                           "since": parse_last_ts(lines)})
    return result


def parse_last_ts(lines: list[str]) -> str:
    """Timestamp of the oldest parseable line read -- the start of the window."""
    for line in lines:
        e = parse(line)
        if e and e["ts"]:
            return e["ts"]
    return ""
