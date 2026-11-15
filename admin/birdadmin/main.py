"""The admin console service.

    Public (needed to sign in):
      GET  /api/health
      GET  /api/auth/status
      POST /api/session/password        only when ADMIN_PASSWORD_LOGIN is on
      POST /api/webauthn/login/options|verify
      DELETE /api/session               sign out
      /login.html and its assets

    Everything else requires a session:
      GET    /api/clips                 PATCH/DELETE /api/clips/{slug}
      POST   /api/clips/{slug}/poster   regenerate the poster at a time
      POST   /api/uploads               start an upload job (metadata)
      PUT    /api/uploads/{id}          the file itself, streamed raw
      GET    /api/jobs                  DELETE /api/jobs/{id}
      GET    /api/settings              PUT /api/settings
      GET    /api/passkeys              PATCH/DELETE /api/passkeys/{id}
      GET    /api/logs                  Caddy access logs (?source=site|admin)
      POST   /api/webauthn/register/options|verify

Deny by default: the middleware gates every path and the public ones are
listed explicitly. State-changing /api requests must carry this console's own
Origin (see auth.origin_ok), on top of SameSite=Strict cookies.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from . import __version__, accesslog, auth, media, passkeys
from .config import config
from .jobs import JobQueue
from .storage import LocalStorage, SpacesStorage, Storage

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("birdadmin")

cfg = config()
_problems = cfg.problems()
if _problems:
    # Fail closed: an admin console with no session key, or a password switch
    # with no hash behind it, must not serve anything.
    raise SystemExit("admin console misconfigured:\n  " + "\n  ".join(_problems))


def _make_storage() -> Storage:
    if cfg.spaces_key:
        return SpacesStorage(key=cfg.spaces_key, secret=cfg.spaces_secret,
                             bucket=cfg.spaces_bucket, region=cfg.spaces_region,
                             public_base=cfg.media_base_url)
    return LocalStorage(Path(cfg.local_storage_dir), "/media")


storage = _make_storage()
jobs = JobQueue(storage, cfg.work_dir, cfg.transcode_preset)
log.info("storage: %s; password login %s", storage.describe(),
         "ON" if cfg.password_login else "off")

app = FastAPI(title="Bird Heaven admin", version=__version__,
              docs_url=None, redoc_url=None, openapi_url=None)

# ---------------------------------------------------------------- plumbing

_PUBLIC_EXACT = {
    "/api/health", "/api/auth/status", "/api/session", "/api/session/password",
    "/api/webauthn/login/options", "/api/webauthn/login/verify",
    "/login.html", "/js/login.js", "/js/webauthn.js", "/css/admin.css", "/img/favicon.svg",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


@app.exception_handler(ApiError)
async def _api_error(_request: Request, err: ApiError) -> JSONResponse:
    return JSONResponse({"error": err.message}, status_code=err.status)


@app.exception_handler(media.MediaError)
async def _media_error(_request: Request, err: media.MediaError) -> JSONResponse:
    return JSONResponse({"error": str(err)}, status_code=400)


def _csp() -> str:
    media_origin = " ".join(o for o in {storage.public_url("").rstrip("/")} if o.startswith("http"))
    return "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        f"img-src 'self' data: blob: {media_origin}".strip(),
        f"media-src 'self' blob: {media_origin}".strip(),
        "connect-src 'self'",
        "frame-src 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    ])


CSP = _csp()


@app.middleware("http")
async def gate(request: Request, call_next):
    path = request.url.path
    is_api = path.startswith("/api/")

    if is_api and request.method not in ("GET", "HEAD", "OPTIONS") and not auth.origin_ok(request):
        return JSONResponse({"error": "Request did not come from this console."}, status_code=403)

    if path not in _PUBLIC_EXACT and auth.session(request) is None:
        if is_api or path.startswith("/media/"):
            return JSONResponse({"error": "Please sign in."}, status_code=401)
        return RedirectResponse("/login.html", status_code=303)

    response: Response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def _transports(raw: list[str] | None) -> list[AuthenticatorTransport] | None:
    out = []
    for t in raw or []:
        try:
            out.append(AuthenticatorTransport(t))
        except ValueError:
            continue
    return out or None


def _descriptors() -> list[PublicKeyCredentialDescriptor]:
    return [PublicKeyCredentialDescriptor(id=passkeys.unb64(c["credential_id"]),
                                          transports=_transports(c.get("transports")))
            for c in passkeys.all_credentials()]


# ---------------------------------------------------------------- auth endpoints

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    s = auth.session(request)
    return {
        "signedIn": s is not None,
        "method": s.get("m") if s else None,
        "passwordLogin": cfg.password_login,
        "passkeys": len(passkeys.all_credentials()),
    }


@app.post("/api/session/password")
def password_login(body: dict = Body(default={})) -> JSONResponse:
    if not cfg.password_login:
        # Indistinguishable from "no such endpoint" while the switch is off.
        return JSONResponse({"error": "Not found."}, status_code=404)
    if auth.throttled():
        raise ApiError(429, "Too many failed attempts. Try again in a few minutes.")
    if not auth.verify_password(str(body.get("password") or "")):
        auth.record_failure()
        raise ApiError(401, "That password is not right.")
    auth.clear_failures()
    response = JSONResponse({"ok": True})
    auth.issue_cookie(response, "password")
    return response


@app.delete("/api/session")
def sign_out() -> JSONResponse:
    response = JSONResponse({"ok": True})
    auth.clear_cookie(response)
    return response


@app.post("/api/webauthn/login/options")
def login_options(request: Request) -> Response:
    if auth.throttled():
        raise ApiError(429, "Too many failed attempts. Try again in a few minutes.")
    descriptors = _descriptors()
    if not descriptors:
        raise ApiError(400, "No passkeys are registered yet.")
    options = generate_authentication_options(
        rp_id=passkeys.rp_id(request),
        user_verification=UserVerificationRequirement.REQUIRED,
        allow_credentials=descriptors,
    )
    response = Response(options_to_json(options), media_type="application/json")
    passkeys.store_challenge(response, options.challenge, "login")
    return response


@app.post("/api/webauthn/login/verify")
def login_verify(request: Request, body: dict = Body(default={})) -> JSONResponse:
    if auth.throttled():
        raise ApiError(429, "Too many failed attempts. Try again in a few minutes.")
    try:
        challenge = passkeys.read_challenge(request, "login")
    except passkeys.CeremonyError as err:
        raise ApiError(400, str(err)) from err
    credential = body.get("credential") or {}
    raw_id = credential.get("rawId") or credential.get("id")
    if not raw_id:
        raise ApiError(400, "That passkey response is incomplete.")
    entry = passkeys.find_by_credential_id(passkeys.unb64(raw_id))
    if not entry:
        auth.record_failure()
        raise ApiError(401, "That passkey is not registered here.")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=passkeys.rp_id(request),
            expected_origin=passkeys.expected_origin(request),
            credential_public_key=passkeys.unb64(entry["public_key"]),
            credential_current_sign_count=entry["sign_count"],
            require_user_verification=True,
        )
    except WebAuthnException as err:
        auth.record_failure()
        log.warning("passkey sign-in failed: %s", err)
        raise ApiError(401, "That passkey could not be verified.") from err
    auth.clear_failures()
    passkeys.mark_used(entry["id"], verified.new_sign_count)
    response = JSONResponse({"ok": True})
    auth.issue_cookie(response, "passkey")
    passkeys.clear_challenge(response)
    return response


# ---------------------------------------------------------------- passkey management

@app.get("/api/passkeys")
def list_passkeys() -> list[dict]:
    return [passkeys.public_view(c) for c in passkeys.all_credentials()]


@app.post("/api/webauthn/register/options")
def register_options(request: Request) -> Response:
    options = generate_registration_options(
        rp_id=passkeys.rp_id(request),
        rp_name=passkeys.RP_NAME,
        user_id=passkeys.user_handle(),
        user_name=passkeys.USER_NAME,
        user_display_name=passkeys.USER_DISPLAY_NAME,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=_descriptors(),
    )
    response = Response(options_to_json(options), media_type="application/json")
    passkeys.store_challenge(response, options.challenge, "register")
    return response


@app.post("/api/webauthn/register/verify")
def register_verify(request: Request, body: dict = Body(default={})) -> JSONResponse:
    try:
        challenge = passkeys.read_challenge(request, "register")
    except passkeys.CeremonyError as err:
        raise ApiError(400, str(err)) from err
    credential = body.get("credential") or {}
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=passkeys.rp_id(request),
            expected_origin=passkeys.expected_origin(request),
            require_user_verification=True,
        )
    except WebAuthnException as err:
        raise ApiError(400, f"That passkey could not be registered: {err}") from err
    entry = passkeys.add(verified.credential_id, verified.credential_public_key,
                         verified.sign_count,
                         (credential.get("response") or {}).get("transports"),
                         " ".join(str(body.get("nickname") or "").split()))
    response = JSONResponse(passkeys.public_view(entry))
    passkeys.clear_challenge(response)
    return response


@app.patch("/api/passkeys/{entry_id}")
def rename_passkey(entry_id: int, body: dict = Body(default={})) -> dict:
    if not passkeys.rename(entry_id, " ".join(str(body.get("nickname") or "").split())):
        raise ApiError(404, "No such passkey.")
    return {"ok": True}


@app.delete("/api/passkeys/{entry_id}")
def delete_passkey(entry_id: int) -> dict:
    creds = passkeys.all_credentials()
    if len(creds) == 1 and creds[0]["id"] == entry_id and not cfg.password_login:
        raise ApiError(409, "That is your only passkey and password login is off, so "
                            "removing it would lock you out. Add another passkey first.")
    if not passkeys.remove(entry_id):
        raise ApiError(404, "No such passkey.")
    return {"ok": True}


# ---------------------------------------------------------------- clips

def _clip_view(v: dict[str, Any]) -> dict[str, Any]:
    out = dict(v)
    out["videoUrl"] = storage.public_url(v["video"]) if v.get("video") else None
    out["posterUrl"] = storage.public_url(v["poster"]) if v.get("poster") else None
    return out


@app.get("/api/clips")
def list_clips() -> dict:
    doc = media.load_catalog(storage)
    videos = sorted(doc["videos"], key=lambda v: v.get("recorded", ""), reverse=True)
    species = sorted({s for v in videos for s in v.get("species", [])} | set(COMMON_SPECIES))
    return {"updated": doc.get("updated"), "videos": [_clip_view(v) for v in videos],
            "species": species, "storage": storage.describe(),
            "publicSite": cfg.public_site_url}


@app.patch("/api/clips/{slug}")
def edit_clip(slug: str, body: dict = Body(default={})) -> dict:
    fields = {k: body[k] for k in media.EDITABLE if k in body}

    def change(doc: dict[str, Any]) -> dict[str, Any]:
        entry = media.find(doc, slug)
        media.apply_edits(entry, fields)
        return dict(entry)
    return _clip_view(media.edit_catalog(storage, change))


@app.delete("/api/clips/{slug}")
def delete_clip(slug: str, deleteFiles: bool = True) -> dict:  # noqa: N803 -- query name
    media.remove_clip(storage, slug, delete_files=deleteFiles)
    return {"ok": True}


@app.post("/api/clips/{slug}/poster")
def new_poster(slug: str, body: dict = Body(default={})) -> dict:
    try:
        at = float(body.get("at"))
    except (TypeError, ValueError) as err:
        raise ApiError(400, "Give the poster time in seconds.") from err
    return _clip_view(media.regenerate_poster(storage, slug, at, cfg.work_dir / "posters"))


# ---------------------------------------------------------------- uploads

@app.post("/api/uploads")
def start_upload(body: dict = Body(default={})) -> dict:
    title = " ".join(str(body.get("title") or "").split())[:120]
    if not title:
        raise ApiError(400, "Give the clip a title.")
    size = body.get("size")
    if isinstance(size, int) and size > cfg.max_upload_bytes:
        raise ApiError(413, f"That file is larger than the "
                            f"{cfg.max_upload_bytes // (1024 * 1024)} MB limit.")
    recorded = media.parse_when(body["recorded"]) if body.get("recorded") else None
    meta = media.ClipMeta(
        title=title,
        description=str(body.get("description") or "").strip()[:1000],
        species=media.clean_species(body.get("species")),
        recorded=recorded,
        featured=bool(body.get("featured")),
    )
    job = jobs.create(str(body.get("filename") or "upload"), meta,
                      size if isinstance(size, int) else None)
    return job.view()


@app.put("/api/uploads/{job_id}")
async def receive_upload(job_id: str, request: Request) -> dict:
    job = jobs.get(job_id)
    if job is None or job.state != "receiving":
        raise ApiError(404, "That upload is not waiting for a file.")
    path = jobs.upload_path(job)
    job.step = "Receiving the file"
    try:
        with path.open("wb") as f:
            async for chunk in request.stream():
                job.received += len(chunk)
                if job.received > cfg.max_upload_bytes:
                    raise ApiError(413, "That file is larger than the upload limit.")
                f.write(chunk)
    except ApiError as err:
        jobs.fail(job, err.message)
        raise
    except Exception as err:  # client went away mid-upload, disk full, ...
        jobs.fail(job, f"The upload did not finish: {err}")
        raise ApiError(400, "The upload did not finish.") from err
    if job.received == 0:
        jobs.fail(job, "The file was empty.")
        raise ApiError(400, "The file was empty.")
    jobs.enqueue(job)
    return job.view()


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    return jobs.list()


@app.delete("/api/jobs/{job_id}")
def dismiss_job(job_id: str) -> dict:
    jobs.dismiss(job_id)
    return {"ok": True}


# ---------------------------------------------------------------- site settings

DEFAULT_SETTINGS = {
    "siteName": "Bird Heaven",
    "tagline": "Live from the backyard feeders",
    "intro": "",
    "location": "",
    "paypalHostedButtonId": "",
    "live": {"status": "offline", "kind": "", "url": "", "title": "Feeder cam"},
}


@app.get("/api/settings")
def get_settings() -> dict:
    saved = media.load_settings(storage)
    merged = {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k != "live"}}
    merged["live"] = {**DEFAULT_SETTINGS["live"], **(saved.get("live") or {})}
    merged["saved"] = bool(saved)
    return merged


@app.put("/api/settings")
def put_settings(body: dict = Body(default={})) -> dict:
    return media.save_settings(storage, media.clean_settings(body))


# ---------------------------------------------------------------- access logs

@app.get("/api/logs")
def access_logs(source: str = "site", limit: int = 200, q: str = "", status: str = "",
                hideApi: bool = False) -> dict:  # noqa: N803 -- query name
    if source not in accesslog.SOURCES:
        raise ApiError(400, "Unknown log.")
    return accesslog.read(cfg.access_log_dir, source, limit=limit, query=q, status=status,
                          hide_api=hideApi)


COMMON_SPECIES = (
    "American Goldfinch", "American Robin", "Black-capped Chickadee", "Blue Jay",
    "Carolina Chickadee", "Carolina Wren", "Dark-eyed Junco", "Downy Woodpecker",
    "Eastern Bluebird", "Eastern Gray Squirrel", "European Starling", "Hairy Woodpecker",
    "House Finch", "House Sparrow", "Mourning Dove", "Northern Cardinal", "Purple Finch",
    "Red-bellied Woodpecker", "Red-winged Blackbird", "Ruby-throated Hummingbird",
    "Song Sparrow", "Tufted Titmouse", "White-breasted Nuthatch", "White-throated Sparrow",
)


# ---------------------------------------------------------------- static

if isinstance(storage, LocalStorage):
    # Development: the local "bucket" is served here (behind the session gate)
    # so thumbnails and previews work without Spaces.
    app.mount("/media", StaticFiles(directory=storage.root), name="media")

app.mount("/", StaticFiles(directory=cfg.static_dir, html=True), name="static")
