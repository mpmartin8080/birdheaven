"""Smoke tests for the admin console. No pytest needed:

    py -3 -m admin.tests.smoke        (from the repo root; needs admin/requirements.txt + httpx)

Uses a throwaway copy of dev/media as local storage. The upload pipeline is
exercised too when ffmpeg is on PATH (it always is inside the admin image).
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "admin"))

from argon2 import PasswordHasher  # noqa: E402

FAILS: list[str] = []
ORIGIN = {"origin": "http://testserver"}
PASSWORD = "correct horse battery staple"
HASH = PasswordHasher().hash(PASSWORD)
KEY = "k" * 48


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def fresh(tmp: Path, **env: str):
    for k in list(os.environ):
        if k.startswith(("ADMIN_", "SPACES_")):
            del os.environ[k]
    os.environ.update({
        "ADMIN_SESSION_KEY": KEY,
        "ADMIN_COOKIE_SECURE": "false",
        "ADMIN_DATA_DIR": str(tmp / "data"),
        "ADMIN_LOCAL_STORAGE_DIR": str(tmp / "media"),
        **env,
    })
    from birdadmin import config as config_mod
    config_mod.config.cache_clear()
    import birdadmin.main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient
    return TestClient(main_mod.app), main_mod


def signed_in(client, method: str):
    """A client carrying a session cookie of the given method."""
    from birdadmin import auth
    from starlette.responses import Response
    r = Response()
    auth.issue_cookie(r, method)
    cookie = r.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    client.cookies.set(auth.COOKIE_NAME, cookie)
    return client


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bh-admin-test-"))
    shutil.copytree(ROOT / "dev" / "media", tmp / "media")
    try:
        run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'FAILED: ' + str(len(FAILS)) if FAILS else 'all passed'}")
    return 1 if FAILS else 0


def run(tmp: Path) -> None:
    print("== fails closed when misconfigured")
    for env, why in (({"ADMIN_SESSION_KEY": "short"}, "short session key"),
                     ({"ADMIN_PASSWORD_LOGIN": "true"}, "switch on without a hash")):
        try:
            fresh(tmp, **env)
            check(False, f"refuses to start: {why}")
        except SystemExit:
            check(True, f"refuses to start: {why}")

    print("== password switch OFF (default), no passkeys")
    c, _ = fresh(tmp, ADMIN_PASSWORD_HASH=HASH)
    st = c.get("/api/auth/status").json()
    check(st == {"signedIn": False, "method": None, "passwordLogin": False, "passkeys": 0},
          "status reports locked-out state")
    check(c.post("/api/session/password", json={"password": PASSWORD}, headers=ORIGIN).status_code == 404,
          "password endpoint does not exist while the switch is off")
    r = c.get("/", follow_redirects=False)
    check(r.status_code == 303 and r.headers["location"] == "/login.html", "/ redirects to sign-in")
    check(c.get("/api/clips").status_code == 401, "API requires a session")
    check(c.get("/media/catalog.json").status_code == 401, "local media requires a session")
    check(c.get("/login.html").status_code == 200 and c.get("/js/login.js").status_code == 200,
          "sign-in page is public")
    check(c.post("/api/webauthn/login/options", headers=ORIGIN).status_code == 400,
          "passkey sign-in explains there are no passkeys")

    print("== password switch ON")
    c, main_mod = fresh(tmp, ADMIN_PASSWORD_LOGIN="true", ADMIN_PASSWORD_HASH=HASH)
    check(c.post("/api/session/password", json={"password": "nope"}, headers=ORIGIN).status_code == 401,
          "wrong password refused")
    check(c.post("/api/session/password", json={"password": PASSWORD}).status_code == 403,
          "missing Origin refused (CSRF)")
    check(c.post("/api/session/password", json={"password": PASSWORD},
                 headers={"origin": "https://evil.example"}).status_code == 403,
          "foreign Origin refused (CSRF)")
    r = c.post("/api/session/password", json={"password": PASSWORD}, headers=ORIGIN)
    check(r.status_code == 200 and "samesite=strict" in r.headers["set-cookie"].lower(),
          "right password signs in with a SameSite=Strict cookie")
    st = c.get("/api/auth/status").json()
    check(st["signedIn"] and st["method"] == "password", "session marked as a password session")
    r = c.get("/")
    check(r.status_code == 200 and "Bird Heaven admin" in r.text, "console loads when signed in")
    csp = r.headers.get("content-security-policy", "")
    check("frame-ancestors 'none'" in csp and "script-src 'self'" in csp, "console has a strict CSP")
    check(r.headers.get("x-robots-tag") == "noindex, nofollow", "console is noindex")

    opts = c.post("/api/webauthn/register/options", headers=ORIGIN)
    body = opts.json()
    check(opts.status_code == 200 and body["rp"]["id"] == "testserver"
          and body["authenticatorSelection"]["residentKey"] == "required",
          "passkey registration options issued for this host")

    print("== served on the site's hostname at port 7777 (production layout)")
    from fastapi.testclient import TestClient
    p = TestClient(main_mod.app, base_url="https://birdheaven.plebe.net:7777")
    good_origin = {"origin": "https://birdheaven.plebe.net:7777"}
    check(p.post("/api/session/password", json={"password": PASSWORD},
                 headers={"origin": "https://birdheaven.plebe.net"}).status_code == 403,
          "a request from the public site's origin (port 443) is refused")
    check(p.post("/api/session/password", json={"password": PASSWORD}, headers=good_origin).status_code == 200,
          "the console's own origin (port 7777) is accepted")
    opts = p.post("/api/webauthn/register/options", headers=good_origin).json()
    check(opts["rp"]["id"] == "birdheaven.plebe.net", "passkey RP ID is the bare hostname (no port)")

    print("== clips")
    clips = c.get("/api/clips").json()
    check(len(clips["videos"]) == 8 and clips["videos"][0]["posterUrl"].startswith("/media/"),
          "lists the catalog with media URLs")
    slug = clips["videos"][0]["slug"]
    r = c.patch(f"/api/clips/{slug}", json={"title": "  New   title ", "species": "Blue Jay, ,Blue Jay, Robin",
                                            "featured": True}, headers=ORIGIN)
    check(r.status_code == 200 and r.json()["title"] == "New title"
          and r.json()["species"] == ["Blue Jay", "Robin"] and r.json()["featured"] is True,
          "edit cleans title/species and sets featured")
    check(c.patch(f"/api/clips/{slug}", json={"title": ""}, headers=ORIGIN).status_code == 400,
          "empty title rejected")
    check(c.patch("/api/clips/nope", json={"title": "x"}, headers=ORIGIN).status_code == 400,
          "unknown clip reported")
    poster = tmp / "media" / clips["videos"][1]["poster"]
    check(c.delete(f"/api/clips/{clips['videos'][1]['slug']}?deleteFiles=false", headers=ORIGIN).status_code == 200
          and poster.exists(), "delete without files keeps the poster")
    check(c.delete(f"/api/clips/{clips['videos'][2]['slug']}?deleteFiles=true", headers=ORIGIN).status_code == 200
          and not (tmp / "media" / clips["videos"][2]["poster"]).exists(), "delete with files removes the poster")
    check(len(c.get("/api/clips").json()["videos"]) == 6, "catalog now has 6 clips")

    print("== site settings")
    s = c.get("/api/settings").json()
    check(s["siteName"] == "Bird Heaven" and s["saved"] is False, "defaults before first save")
    good = {"siteName": "Bird Heaven", "tagline": "Hi", "intro": "Welcome", "location": "Ohio",
            "paypalHostedButtonId": "ABC123",
            "live": {"status": "live", "kind": "hls", "url": "https://x.example/a.m3u8", "title": ""}}
    r = c.put("/api/settings", json=good, headers=ORIGIN)
    check(r.status_code == 200 and r.json()["live"]["title"] == "Feeder cam", "valid settings saved")
    saved = json.loads((tmp / "media" / "site-settings.json").read_text())
    check(saved["paypalHostedButtonId"] == "ABC123", "written to site-settings.json in storage")
    for bad, why in ((dict(good, paypalHostedButtonId="x y"), "bad PayPal id"),
                     (dict(good, live={"status": "live", "kind": "", "url": ""}), "live without URL"),
                     (dict(good, live={"status": "offline", "kind": "hls", "url": "javascript:alert(1)"}), "unsafe URL"),
                     (dict(good, siteName=" "), "blank name")):
        check(c.put("/api/settings", json=bad, headers=ORIGIN).status_code == 400, f"rejects {why}")

    print("== passkey guard and switch-off")
    from birdadmin import passkeys
    entry = passkeys.add(b"cred-1", b"pk", 0, ["internal"], "Test key")
    check(len(c.get("/api/passkeys").json()) == 1, "passkey listed")
    c2, _ = fresh(tmp, ADMIN_PASSWORD_HASH=HASH)          # switch OFF again
    st = c2.get("/api/auth/status").json()
    check(st["passkeys"] == 1 and not st["passwordLogin"], "status sees the passkey")
    c2.cookies.update(c.cookies)
    check(c2.get("/api/clips").status_code == 401, "switching password login off ends password sessions")
    signed_in(c2, "passkey")
    r = c2.delete(f"/api/passkeys/{entry['id']}", headers=ORIGIN)
    check(r.status_code == 409, "cannot delete the only passkey while password login is off")
    r = c2.post("/api/webauthn/login/options", headers=ORIGIN)
    check(r.status_code == 200 and len(r.json()["allowCredentials"]) == 1, "sign-in options list the passkey")
    r = c2.post("/api/webauthn/login/verify", json={"credential": {"id": "AAAA", "rawId": "AAAA"}},
                headers=ORIGIN, cookies={passkeys.CHALLENGE_COOKIE: r.cookies.get(passkeys.CHALLENGE_COOKIE)})
    check(r.status_code == 401, "unknown credential refused")

    print("== upload pipeline")
    if not shutil.which("ffmpeg"):
        print("  skip: ffmpeg not on PATH (run inside the admin image to cover this)")
        return
    clip = tmp / "test.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "4", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)], check=True)
    c3 = signed_in(fresh(tmp, ADMIN_PASSWORD_HASH=HASH)[0], "passkey")
    job = c3.post("/api/uploads", json={"filename": "test.mp4", "size": clip.stat().st_size,
                                        "title": "Test pattern", "species": "Blue Jay",
                                        "recorded": "2026-11-15T08:30:00Z"}, headers=ORIGIN).json()
    r = c3.put(f"/api/uploads/{job['id']}", content=clip.read_bytes(),
               headers={**ORIGIN, "content-type": "application/octet-stream"})
    check(r.status_code == 200 and r.json()["state"] == "queued", "file received and queued")
    for _ in range(60):
        jobs = {j["id"]: j for j in c3.get("/api/jobs").json()}
        if jobs[job["id"]]["state"] in ("done", "error"):
            break
        time.sleep(0.5)
    j = jobs[job["id"]]
    check(j["state"] == "done", f"job finished: {j['state']} {j.get('error') or ''}")
    new = next((v for v in c3.get("/api/clips").json()["videos"] if v["slug"] == j["slug"]), None)
    check(new is not None and new["video"] == "videos/2026/11/2026-11-15-test-pattern.mp4"
          and (tmp / "media" / new["video"]).exists() and (tmp / "media" / new["poster"]).exists(),
          "published: video + poster stored, catalog updated")
    check(new is not None and new["width"] == 1280 and 3.5 < new["duration"] < 4.5, "metadata probed")
    r = c3.post(f"/api/clips/{j['slug']}/poster", json={"at": 2.0}, headers=ORIGIN)
    check(r.status_code == 200 and r.json()["poster"] != new["poster"]
          and not (tmp / "media" / new["poster"]).exists(), "poster regenerated under a new key")
    job2 = c3.post("/api/uploads", json={"filename": "x.mp4", "title": "Junk"}, headers=ORIGIN).json()
    c3.put(f"/api/uploads/{job2['id']}", content=b"not a video", headers=ORIGIN)
    for _ in range(20):
        j2 = {x["id"]: x for x in c3.get("/api/jobs").json()}[job2["id"]]
        if j2["state"] in ("done", "error"):
            break
        time.sleep(0.5)
    check(j2["state"] == "error" and j2["error"], f"junk upload fails with a message: {j2.get('error', '')[:60]}")


if __name__ == "__main__":
    raise SystemExit(main())
