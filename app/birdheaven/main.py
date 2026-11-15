"""The web service: the front page, a few small JSON endpoints, and headers.

    GET /              webroot/index.html
    GET /api/config    site name, tagline, PayPal button id (public values only)
    GET /api/videos    the recordings catalog, absolute media URLs
    GET /api/live      live stream status (stub -- see live.py)
    POST /api/contact  the contact form (see contact.py)
    GET /api/health    liveness for Caddy / deploy status

Video bytes never pass through here; browsers fetch them from the Spaces CDN.
Site text, PayPal and live settings come from sitesettings.py: env defaults,
overlaid by the site-settings.json the admin console writes.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__, contact, live
from .catalog import CatalogCache
from .config import config, origin_of
from .sitesettings import SettingsCache

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

cfg = config()
catalog = CatalogCache(cfg)
settings = SettingsCache(cfg)

CONTACT_MAX_BODY = 32 * 1024

app = FastAPI(title="Bird Heaven", version=__version__,
              docs_url=None, redoc_url=None, openapi_url=None)


@lru_cache(maxsize=32)
def _csp(live_origin: str, live_is_iframe: bool) -> str:
    """Content-Security-Policy, exact to the media origin and the current live URL.

    Cached per (live origin, kind), since the live settings can change at
    runtime from the admin console. No inline script or style anywhere in
    webroot/, so neither 'unsafe-inline' nor a nonce is needed. hls.js comes
    from cdnjs and is loaded only when a live HLS stream is actually on.
    """
    media = " ".join(sorted(o for o in {cfg.media_origin, live_origin} if o))
    frame = live_origin if live_is_iframe and live_origin else "'none'"
    return "; ".join([
        "default-src 'self'",
        "script-src 'self' https://cdnjs.cloudflare.com",
        "style-src 'self' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        f"img-src 'self' data: {media}".strip(),
        f"media-src 'self' blob: {media}".strip(),
        f"connect-src 'self' {media}".strip(),
        f"frame-src {frame}",
        "form-action 'self' https://www.paypal.com",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    ])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    path = request.url.path
    if not path.startswith("/api/"):
        lv = settings.get()["live"]
        response.headers.setdefault("Content-Security-Policy",
                                    _csp(origin_of(lv["url"]), lv["kind"] == "iframe"))
    else:
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "public, max-age=30")
    elif path.startswith(("/css/", "/js/", "/img/")):
        response.headers.setdefault("Cache-Control", "public, max-age=3600")
    else:
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.get("/api/health")
def health() -> JSONResponse:
    # Healthy even with an empty/unreachable catalog: the page still works and
    # says so. The catalog state is reported, not enforced.
    catalog.get()  # ensure at least one fetch has happened (cached otherwise)
    return JSONResponse({"status": "ok", "version": __version__,
                         "catalog": "ok" if catalog.healthy else "unavailable"},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/config")
def site_config() -> dict:
    s = settings.get()
    return {
        "siteName": s["siteName"],
        "tagline": s["tagline"],
        "intro": s["intro"],
        "location": s["location"],
        "paypalHostedButtonId": s["paypalHostedButtonId"] or None,
    }


@app.post("/api/contact")
async def contact_form(request: Request) -> JSONResponse:
    no_store = {"Cache-Control": "no-store"}

    def fail(message: str, status: int) -> JSONResponse:
        return JSONResponse({"ok": False, "error": message}, status_code=status, headers=no_store)

    # JSON only (a cross-site page can't send that without a CORS preflight,
    # which this API never answers), from this site's own pages, and small.
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return fail("Please send the form as JSON.", 415)
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
        return fail("Cross-site form posts are not accepted.", 403)
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > CONTACT_MAX_BODY:
            return fail("That message is too long.", 413)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return fail("Please send the form as JSON.", 400)

    client_ip = request.client.host if request.client else "unknown"
    try:
        # Sending can take seconds; keep it off the event loop.
        await run_in_threadpool(contact.handle, cfg, settings.get()["siteName"], body, client_ip)
    except contact.ContactError as err:
        return fail(str(err), err.status)
    return JSONResponse({"ok": True}, headers=no_store)


@app.get("/api/videos")
def videos() -> dict:
    items, updated = catalog.get()
    return {"updated": updated, "count": len(items), "videos": items}


@app.get("/api/live")
def live_status() -> JSONResponse:
    return JSONResponse(live.status(settings.get()),
                        headers={"Cache-Control": "public, max-age=15"})


# Local development only: when the catalog is a local file and no media URL is
# configured, serve the catalog's directory (dev/media, laid out like the
# bucket) at /media. Never mounted in production, where MEDIA_BASE_URL is set.
if not cfg.media_base_url and not cfg.catalog_source.startswith(("http://", "https://")):
    _local_media = Path(cfg.catalog_source).parent
    if _local_media.is_dir():
        app.mount("/media", StaticFiles(directory=_local_media), name="media")

app.mount("/", StaticFiles(directory=cfg.static_dir, html=True), name="static")
