"""The web service: the front page, three small JSON endpoints, and headers.

    GET /              webroot/index.html
    GET /api/config    site name, tagline, PayPal button id (public values only)
    GET /api/videos    the recordings catalog, absolute media URLs
    GET /api/live      live stream status (stub -- see live.py)
    GET /api/health    liveness for Caddy / deploy status

Video bytes never pass through here; browsers fetch them from the Spaces CDN.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, live
from .catalog import CatalogCache
from .config import config

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

cfg = config()
catalog = CatalogCache(cfg)

app = FastAPI(title="Bird Heaven", version=__version__,
              docs_url=None, redoc_url=None, openapi_url=None)


def _csp() -> str:
    """Content-Security-Policy, built from config so the media origin is exact.

    No inline script or style anywhere in webroot/, so neither 'unsafe-inline'
    nor a nonce is needed. hls.js comes from cdnjs and is loaded only when a
    live HLS stream is actually on.
    """
    media = " ".join(o for o in {cfg.media_origin, cfg.live_origin} if o)
    frame = cfg.live_origin if cfg.live_kind == "iframe" and cfg.live_origin else "'none'"
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


CSP = _csp()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    path = request.url.path
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
    return {
        "siteName": cfg.site_name,
        "tagline": cfg.site_tagline,
        "location": cfg.location_label,
        "paypalHostedButtonId": cfg.paypal_hosted_button_id or None,
    }


@app.get("/api/videos")
def videos() -> dict:
    items, updated = catalog.get()
    return {"updated": updated, "count": len(items), "videos": items}


@app.get("/api/live")
def live_status() -> JSONResponse:
    return JSONResponse(live.status(cfg), headers={"Cache-Control": "public, max-age=15"})


# Local development only: serve dev/media/ so the sample catalog can point at
# real files without a bucket. Never mounted when MEDIA_BASE_URL is set.
_dev_media = cfg.static_dir.parent / "dev" / "media"
if not cfg.media_base_url and _dev_media.is_dir():
    app.mount("/media", StaticFiles(directory=_dev_media), name="media")

app.mount("/", StaticFiles(directory=cfg.static_dir, html=True), name="static")
