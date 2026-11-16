# Bird Heaven — Architecture

A public website for sharing live and recorded video of the birds visiting one
backyard. Anyone can watch; only the owner publishes, through a
passkey-protected admin console.

```
                         ┌──────────────────────── DigitalOcean ─────────────────────────┐
                         │  Droplet (Debian 13 + Docker)                                  │
  visitor's browser      │  ┌──────────────────────────────────────────────────────────┐ │
   https://<domain>/ ────┼─▶│ caddy :80 :443 :7777  (one SAN cert <- certs: lego)      │ │
                         │  │   ├─ <domain>:443    ─▶ web :8000   (FastAPI, no secrets)│ │
  owner's browser        │  │   │                      /, /api/videos, /api/live, ...  │ │
   https://<dom>:7777/ ──┼─▶│   └─ <domain>:7777   ─▶ admin :8000 (FastAPI + ffmpeg)   │ │
        │                │  │                          passkeys, uploads, settings     │ │
        │                │  │                          volume: admin-data (/data)      │ │
        │                │  │  [future] live ingest (e.g. MediaMTX)                    │ │
        │                │  └───────────────┬──────────────────────┬───────────────────┘ │
        │                │       reads JSON │                      │ writes objects      │
        │                │                  ▼                      ▼                     │
        │  <video src>   │  Spaces bucket ──▶ Spaces CDN                                  │
        └────────────────┼─▶  catalog.json, site-settings.json                           │
   (visitors too)        │    videos/YYYY/MM/<slug>.mp4, posters/YYYY/MM/<slug>.jpg       │
                         │  (no registry: all images built on the droplet over SSH)       │
                         └────────────────────────────────────────────────────────────────┘
                                           ▲
  owner's workstation (Windows) ───────────┘  deploy-droplet deploy   (code + config)
                                              python -m tools.publish (optional CLI)
```

## Components

| Piece | What it is | Where it lives |
|---|---|---|
| `webroot/` | The public front page: plain HTML/CSS/JS, no build step | Baked into the `web` image |
| `app/birdheaven/` | Public FastAPI service: page, catalog API, live-status stub, site settings overlay, contact form, security headers. Its only secret is the SMTP password. | `web` container |
| `admin/birdadmin/` + `admin/static/` | Admin console: passkey sign-in, upload/edit/delete clips, regenerate posters, edit site settings, manage passkeys, view access logs | `admin` container, at `https://<domain>:7777/` |
| Caddy | TLS on :443 and :7777 with the one SAN certificate, alias→primary and HTTP→HTTPS redirects, ACME challenge webroot on :80, compression, upload size backstop, JSON access logs for both sites | `caddy` container |
| `deploy/certs/` | lego (ACME client): one Let's Encrypt certificate with every site hostname as a SAN, renewed automatically | `certs` container |
| Spaces + CDN | Videos, posters, `catalog.json`, `site-settings.json` | DigitalOcean Spaces |
| `deploy/` | Workstation CLI: bootstrap, build both images on the droplet, `compose up`, certificates, rollback, prune, admin password | Your machine only |
| `tools/publish.py` | Optional command-line publishing; same code as the admin console | Your machine only |

The deployment mechanics come from `../financesite`: images are built on the
droplet's own Docker daemon via `DOCKER_HOST=ssh://` and started with
`docker compose -f app/compose.yaml -f deploy/compose.prod.yaml up -d`. Secrets
and per-machine settings live in a gitignored `deploy/.env.prod`, optionally
as `op://` 1Password references.

**No container registry.** The DigitalOcean account allows one registry and
financesite uses it. Since the build already runs on the droplet's daemon, the
images simply stay in its local store as `birdheaven-web:<sha>` and
`birdheaven-admin:<sha>`, and compose runs them with `pull_policy: never`. Old
tags stay for rollback (`deploy-droplet images`, `up --tag <sha>`) until
`deploy-droplet prune`. If the droplet is rebuilt, `deploy` rebuilds from source.

## Storage strategy: DigitalOcean Spaces with the built-in CDN

**Decision:** recorded video is stored as objects in a Spaces bucket and served
to browsers straight from the Spaces CDN endpoint. Viewers never pull video
through the droplet. (Uploads do pass through it, via the admin console.)

| Option | Verdict |
|---|---|
| **Spaces + CDN** | **Chosen.** $5/mo base covers 250 GiB stored + 1 TiB egress, then ~$0.02/GiB stored and ~$0.01/GiB out. CDN included. Video scales independently of the droplet. S3-compatible. |
| Block-storage Volume on the droplet | Rejected. $0.10/GiB/mo (5× Spaces), every view streams through the droplet, no edge cache. |
| Droplet root disk | Rejected. Small, tied to the droplet's lifecycle, same bandwidth problem. |
| YouTube / Vimeo embeds | Rejected for now: their player, ads and recommendations. Easy to add later as a mirror. |
| Managed video (Mux, Cloudflare Stream) | Overkill for a backyard archive; per-minute pricing. |

**File format:** H.264 + AAC in MP4 with `+faststart`, so playback starts
immediately and seeking uses HTTP range requests. Every browser plays it
natively. Clips that are already H.264 (most cameras and phones) are only
*remuxed*, which takes seconds even on the droplet; other formats are
transcoded with x264 `veryfast` (CRF 23, capped at 1080p).

**Bucket layout:**

```
<bucket>/
  catalog.json                ← every published clip            (Cache-Control: 30 s)
  site-settings.json          ← written by the admin console     (Cache-Control: 30 s)
  videos/YYYY/MM/<slug>.mp4   ← public-read, immutable           (Cache-Control: 1 year)
  posters/YYYY/MM/<slug>.jpg  ← public-read, immutable; a regenerated poster gets a new key
```

**The catalog** is a JSON document, not a database. One owner publishing a
handful of clips a week does not need Postgres:

```json
{
  "version": 1,
  "updated": "2026-11-14T16:02:11Z",
  "videos": [
    {
      "slug": "2026-11-14-cardinal-pair",
      "title": "Cardinal pair at the platform feeder",
      "description": "The male hands off a sunflower seed.",
      "species": ["Northern Cardinal"],
      "recorded": "2026-11-14T07:42:00-05:00",
      "duration": 48.2,
      "video": "videos/2026/11/2026-11-14-cardinal-pair.mp4",
      "poster": "posters/2026/11/2026-11-14-cardinal-pair.jpg",
      "width": 1920, "height": 1080, "bytes": 3812455, "featured": true
    }
  ]
}
```

The `web` service fetches the catalog and settings server-side, caches them
for 60 s, validates them, and serves `/api/videos` with absolute URLs. A
malformed document degrades to "fewer clips" or "default settings", never a
broken page. Browsers load `<video>`/`<img>` straight from the CDN (no CORS
needed). Only the admin console writes these documents. It serialises its
own writes with a lock; running `tools/publish.py` at the same moment as an
admin upload is the one race left, and it is unlikely with one owner.

**Cost guardrails:** the donate button exists because egress is the one
variable cost. 1 TiB/month is ~250,000 one-minute clip views; a DigitalOcean
billing alert is the backstop (see `deploy/README.md`).

## TLS: one certificate, every hostname a SAN

`BIRD_DEPLOY_SITE_DOMAIN` lists the public hostnames, comma-separated. The
first is the **primary**: the public site and the admin console live there.
Every other name 301-redirects to it, on `:80` and on HTTPS. All of them go
on **one** Let's Encrypt certificate as SANs.

Caddy's automatic HTTPS (financesite's mechanism) always issues one
certificate per hostname, so issuance moved to a `certs` container:

1. `certs` runs lego with HTTP-01. lego writes each challenge into the shared
   `acme-webroot` volume. Caddy serves `/.well-known/acme-challenge/` from it
   on `:80` for every name, ahead of any redirect.
2. lego installs `site.crt`/`site.key` in the `certs` volume, which Caddy
   mounts read-only and serves on every HTTPS listener. Caddy's own automatic
   HTTPS is off.
3. Caddy's entrypoint (`deploy/caddy/run.sh`) reloads Caddy through its admin
   API when the certificate file changes. That API listens on loopback inside
   the caddy container, so other containers can't reach it. Renewals therefore
   need no restart.
4. **First boot:** `certs` writes a 7-day self-signed placeholder covering all
   names. Caddy waits for the certs container's health check (the file exists),
   starts, and then the real certificate replaces the placeholder.
5. **Changing the hostname list** issues a fresh certificate: `certs` compares
   the names it last installed with the configured list.
6. **Renewal:** checked twice a day, within 30 days of expiry. Failures back
   off from 15 minutes to 6 hours.

`deploy-droplet cert` verifies all of this end to end (see
`deploy/README.md`, TLS). The whole flow, including adding a hostname, was
tested locally against Pebble, Let's Encrypt's test CA.

## Admin console

A second container, served by Caddy on the site's primary hostname and IP at
**port 7777** (`https://<domain>:7777/`; `BIRD_DEPLOY_ADMIN_PORT`). It uses
the same certificate and needs no extra DNS. Aliases don't serve it, since
passkeys are bound to one hostname. The port can be firewalled to the
owner's IP, since Let's Encrypt never needs to reach it.

**What a separate port does and doesn't isolate.** A different port is a
different *origin*, so the browser's same-origin policy stops pages on the
public site from reading anything the console returns. The console also
rejects every state-changing request whose `Origin` isn't exactly
`https://<domain>:7777` (tested), so a page on `:443` can't act on it either.
Passkeys are bound to the hostname (RP ID `<domain>`), and each signature
covers the exact origin including the port, so an assertion made on `:443`
is useless at `:7777`. **Cookies, though, are scoped by hostname, not port**:
the browser also sends the console's session cookie (`HttpOnly`, so scripts
can't read it) with requests to the public site. That matters only if the
`web` container itself were compromised; it could then capture the cookie of
an owner browsing the public site while signed in. A separate hostname
(`admin.<domain>`) would close that gap. On a single public IP and hostname,
the mitigations are short sessions (8 h), signing out when done, and
firewalling 7777 to your own IP.

**What it does:**

- **Clips:** list with thumbnails, search, feature/unfeature (featured clips
  appear when the live camera is off), edit title/species/date/description,
  set the poster from any frame of the video, delete (optionally removing the
  files from the bucket).
- **Upload:** drag-and-drop with a progress bar. The browser streams the file
  to the console (`PUT`, raw body); a single background worker probes it,
  remuxes or transcodes, grabs a poster, uploads both to Spaces and adds the
  catalog entry. Several uploads can be queued; status is polled. The default
  limit is 4 GiB (`ADMIN_MAX_UPLOAD_MB`).
- **Site settings:** name, tagline, intro paragraph, location, PayPal button
  ID, and the live-stream fields. These are saved as `site-settings.json` in
  the bucket, and the public site picks them up within about a minute, with
  no redeploy. The `.env.prod` values become defaults for anything left blank.
- **Passkeys:** add (one per device), list, remove.
- **Logs:** the HTTP access log of the public site (ports 80 and 443) and of
  the console itself (7777), newest first, filterable by text, status class
  and `/api/` noise, with optional auto-refresh. Caddy writes them as JSON
  lines to the `caddy-logs` volume (rotated at 10 MiB, 5 kept), which the
  console mounts **read-only**. Only the tail of each file is read. The
  console never gets the Docker socket: that would hand the internet-facing
  container root on the droplet.
  A third view, **Sign-ins**, is the console's own authentication log
  (`authlog.py`). It records sign-ins, failures with their reason, throttling,
  password attempts while the switch is off, sign-outs, and passkey changes,
  each with the IP and browser. It is JSON lines in `/data/auth-log.jsonl`
  (one rolled copy at 1 MiB). Writing to it never blocks a sign-in, and
  passwords are never recorded.

**Authentication** follows financesite's WebAuthn design (py_webauthn,
discoverable credentials, user verification required, the challenge in a
short-lived signed cookie, the RP ID taken from the host that served the
request). Sessions are signed, timed cookies (8 h), `HttpOnly`, `Secure`,
`SameSite=Strict`. Every state-changing `/api` request must also carry the
console's own `Origin`, which gives CSRF protection without tokens. Failed
sign-ins are throttled (10 per 15 min). Passkeys (public keys only) are stored
in `/data/passkeys.json` on the `admin-data` volume.

**The recovery switch:** `ADMIN_PASSWORD_LOGIN` in `deploy/.env.prod`.

| Switch | Behaviour |
|---|---|
| `false` (default) | Passkeys only. The password endpoint returns 404; the hash may stay in the file. Any password session still open is invalidated. |
| `true` | The sign-in page also accepts the recovery password (Argon2id hash in `ADMIN_PASSWORD_HASH`). The console shows a standing warning until the switch is off again. |

Use `true` to register the first passkey, or to recover after losing every
passkey (or the `admin-data` volume). Then set it back to `false`.
`deploy-droplet admin-password` sets the hash and session key. The console
refuses to start with no session key, or with the switch on and no valid hash;
it fails closed. It also refuses to delete your last passkey while the switch
is off, which would lock you out.

**Container:** `python:3.11-slim` with Debian's ffmpeg (so not distroless,
unlike `web`), runs as uid 10001, publishes no port itself, and only Caddy
reaches it (Caddy publishes 7777 and proxies to it). It holds the important
secrets: the session key, the password hash, and the Spaces key. (`web` holds
only the contact form's SMTP password.)

## Live video: a stub with a clear seam

`GET /api/live` returns `{"status": "offline" | "live", "kind", "url", "title"}`.
Today it reflects the live fields in site settings: the admin console, or the
`LIVE_*` env defaults. The player understands `hls` (native or hls.js),
`video` (any `<video>`-playable URL) and `iframe` (YouTube Live, Owncast). The
public CSP is computed per request so an embed's origin is allowed only while
it is configured.

| Path | Camera → | Browser gets | Notes |
|---|---|---|---|
| **MediaMTX** on the droplet | RTSP/RTMP/SRT push | HLS (and/or WebRTC via WHEP) | One more compose service. Most flexible. |
| **Owncast** on the droplet | RTMP push from OBS | HLS + chat | Batteries-included; heavier. |
| **YouTube Live** | RTMP push | iframe | Zero server cost; their platform. Works today via the admin console. |

When one is chosen: add the service to `deploy/compose.prod.yaml`, add a Caddy
route (e.g. `handle_path /live/* { reverse_proxy mediamtx:8888 }`), and replace
`app/birdheaven/live.py` with a real probe so `status` flips automatically.

## Donations

A PayPal **hosted Donate button**, rendered as a plain HTML `<form>` that
posts to `https://www.paypal.com/donate`. No PayPal JavaScript runs, which
keeps the CSP tight. The button ID is set in the admin console (or
`PAYPAL_HOSTED_BUTTON_ID`); the support panel is hidden if it is empty.

## Contact form

A plain form on the front page that `POST`s JSON to `/api/contact`. The web
service emails it over SMTP to `CONTACT_EMAIL` (default
`bird.heaven.admin@gmail.com`), with the visitor's address as `Reply-To`. The
destination is never sent to the browser. Without `SMTP_HOST`, messages are
written to the `web` container's log instead, so none are lost before mail is
set up.

**DigitalOcean blocks outbound SMTP** (ports 25, 465 and 587) on new
droplets. Either ask DO support to lift the block (Gmail with an app password
then works), or use a relay that accepts port 2525 (SMTP2GO, Mailgun,
SendGrid, Brevo). See `deploy/.env.example`.

**Abuse controls:**
- A hidden honeypot field.
- A minimum time on the page (3 s).
- Rate limits: 5 messages per IP per hour, 100 per day site-wide.
- Length limits and a 32 KB body cap.
- Header-injection-safe fields.
- A JSON-only, same-origin check.

Bot submissions get the same "sent" reply as real ones.

## Security posture

- **Public site:** read-only, no login, no database, no storage credentials;
  its one secret is the SMTP password. Distroless, non-root. Strict CSP (scripts from self, plus cdnjs
  for hls.js only; media from the configured origin; forms to PayPal;
  `frame-ancestors 'none'`).
- **Admin console:** deny-by-default auth gate with an explicit public-path
  list. Passkeys with user verification. The password exists only behind the
  switch. Origin check plus `SameSite=Strict`. `noindex`. Strict CSP with no
  inline script or style. A non-root container. Settings input is validated
  server-side (PayPal ID charset; https-only live URLs).
- **Network:** Caddy is the only published container, on 80, 443 and 7777.
  Ports 80 and 443 must be open to the internet for Let's Encrypt; 7777 can
  and should be limited to the owner's IP (see `deploy/README.md`, Firewall).

## Hosting note: separate droplet vs. sharing financesite's

Both stacks publish Caddy on :80/:443, so they can't simply run side by side
on one droplet. The default is a **separate droplet** (the $6 size is
enough). To co-host, drop `caddy` from `deploy/compose.prod.yaml`, attach `web`
and `admin` to the finance stack's network, and add the site block and the
`:7777` admin block to financesite's Caddyfile (financesite already uses 6444
for its webhook, so there's no clash).

## Repository layout

```
ARCHITECTURE.md        this file
README.md              quick start
deploy-droplet.bat     cmd.exe launcher for python -m deploy
app/                   public site service (web)
  Dockerfile           distroless runtime, build context = repo root
  compose.yaml         local dev: web on :8000, admin on :8001
  birdheaven/          main, config, catalog, sitesettings, live
  tests/smoke.py       py -3 -m app.tests.smoke
admin/                 admin console service (admin)
  Dockerfile           slim + ffmpeg, non-root
  birdadmin/           main (routes), auth, passkeys, media, storage, jobs, hashpw
  static/              console + sign-in pages
  tests/smoke.py       py -3 -m admin.tests.smoke
webroot/               the public front page
deploy/                droplet deployment CLI (adapted from financesite)
tools/publish.py       command-line publishing (shares admin/birdadmin/media.py)
dev/media/             local "bucket" for development: catalog.json, posters/
```
