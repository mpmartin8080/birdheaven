"""Smoke tests for the web service. No pytest needed:

    py -3 -m app.tests.smoke          (from the repo root; needs fastapi + httpx)
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def fresh_app(**env: str):
    for k in ("CATALOG_URL", "SETTINGS_URL", "MEDIA_BASE_URL", "PAYPAL_HOSTED_BUTTON_ID",
              "LIVE_STATUS", "LIVE_KIND", "LIVE_URL"):
        os.environ.pop(k, None)
    os.environ.update(env)
    from birdheaven import config as config_mod
    config_mod.config.cache_clear()
    import birdheaven.main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient
    return TestClient(main_mod.app)


def main() -> int:
    print("== default (dev catalog, offline, no PayPal)")
    c = fresh_app()
    r = c.get("/")
    check(r.status_code == 200 and "Bird Heaven" in r.text, "GET / serves the page")
    csp = r.headers.get("content-security-policy", "")
    check("frame-ancestors 'none'" in csp and "https://www.paypal.com" in csp, "CSP present, allows PayPal form")
    check(c.get("/api/health").json()["status"] == "ok", "health ok")
    v = c.get("/api/videos").json()
    check(v["count"] == 8, f"dev catalog has 8 clips (got {v['count']})")
    check(v["videos"][0]["poster"].startswith("/media/posters/"), "relative paths map to /media in dev")
    rec = [x["recorded"] for x in v["videos"]]
    check(rec == sorted(rec, reverse=True), "newest first")
    check(c.get(v["videos"][0]["poster"]).status_code == 200, "dev poster is served")
    check(c.get("/api/live").json()["status"] == "offline", "live defaults to offline")
    check(c.get("/api/config").json()["paypalHostedButtonId"] is None, "no PayPal id by default")
    check(c.get("/css/site.css").status_code == 200 and c.get("/js/site.js").status_code == 200, "assets served")

    print("== configured (remote-style media base, live HLS, PayPal)")
    with tempfile.TemporaryDirectory() as tmp:
        cat = Path(tmp) / "catalog.json"
        cat.write_text(json.dumps({"videos": [
            {"slug": "a", "title": "A", "video": "videos/a.mp4", "poster": "posters/a.jpg",
             "recorded": "2026-01-01T00:00:00Z", "species": ["Blue Jay", 7]},
            {"title": "missing slug"},
            "garbage",
        ]}), encoding="utf-8")
        c = fresh_app(CATALOG_URL=str(cat),
                      MEDIA_BASE_URL="https://birds.nyc3.cdn.digitaloceanspaces.com/",
                      PAYPAL_HOSTED_BUTTON_ID="ABC123",
                      LIVE_STATUS="live", LIVE_KIND="hls", LIVE_URL="https://live.example.com/feeder.m3u8")
        v = c.get("/api/videos").json()
        check(v["count"] == 1, "malformed entries skipped")
        check(v["videos"][0]["video"] == "https://birds.nyc3.cdn.digitaloceanspaces.com/videos/a.mp4", "absolute media URL")
        check(v["videos"][0]["species"] == ["Blue Jay"], "non-string species dropped")
        live = c.get("/api/live").json()
        check(live["status"] == "live" and live["kind"] == "hls", "live stub reports configured stream")
        check(c.get("/api/config").json()["paypalHostedButtonId"] == "ABC123", "PayPal id exposed")
        csp = c.get("/").headers["content-security-policy"]
        check("media-src 'self' blob:" in csp and "birds.nyc3.cdn.digitaloceanspaces.com" in csp
              and "live.example.com" in csp, "CSP includes media + live origins")
        check(c.get("/media/posters/x.svg").status_code == 404, "dev /media not mounted when MEDIA_BASE_URL set")

    print("== site-settings.json overrides env defaults")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "catalog.json").write_text('{"videos": []}', encoding="utf-8")
        (Path(tmp) / "site-settings.json").write_text(json.dumps({
            "siteName": "Feeder Live", "tagline": "Birds!", "intro": "Hello there.",
            "paypalHostedButtonId": "XYZ789",
            "live": {"status": "live", "kind": "iframe", "url": "https://embed.example.org/x",
                     "title": "Platform cam"},
        }), encoding="utf-8")
        c = fresh_app(CATALOG_URL=str(Path(tmp) / "catalog.json"),
                      PAYPAL_HOSTED_BUTTON_ID="ENVDEFAULT", SITE_NAME="Env Name")
        conf = c.get("/api/config").json()
        check(conf["siteName"] == "Feeder Live" and conf["intro"] == "Hello there.",
              "name and intro come from the settings file")
        check(conf["paypalHostedButtonId"] == "XYZ789", "PayPal id from file beats env")
        live = c.get("/api/live").json()
        check(live == {"status": "live", "kind": "iframe", "url": "https://embed.example.org/x",
                       "title": "Platform cam"}, "live stub driven by settings file")
        csp = c.get("/").headers["content-security-policy"]
        check("frame-src https://embed.example.org" in csp, "CSP frame-src follows the live embed")

        (Path(tmp) / "site-settings.json").write_text(json.dumps({
            "siteName": "", "paypalHostedButtonId": "bad id!", "live": {"url": "javascript:x"},
        }), encoding="utf-8")
        c = fresh_app(CATALOG_URL=str(Path(tmp) / "catalog.json"),
                      PAYPAL_HOSTED_BUTTON_ID="ENVDEFAULT", SITE_NAME="Env Name")
        conf = c.get("/api/config").json()
        check(conf["siteName"] == "Env Name", "blank name falls back to env")
        check(conf["paypalHostedButtonId"] == "ENVDEFAULT", "invalid PayPal id falls back to env")
        check(c.get("/api/live").json()["status"] == "offline", "unsafe live URL ignored")

    print("== live misconfigured stays offline")
    c = fresh_app(LIVE_STATUS="live", LIVE_KIND="rtmp", LIVE_URL="rtmp://x")
    check(c.get("/api/live").json()["status"] == "offline", "unknown kind -> offline")

    print("== unreachable catalog degrades gracefully")
    c = fresh_app(CATALOG_URL="http://127.0.0.1:9/catalog.json")
    r = c.get("/api/videos")
    check(r.status_code == 200 and r.json()["count"] == 0, "empty list, not an error")
    check(c.get("/api/health").json()["catalog"] == "unavailable", "health reports catalog unavailable")

    print(f"\n{'FAILED: ' + str(len(FAILS)) if FAILS else 'all passed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
