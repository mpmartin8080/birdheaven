# Bird Heaven — Architecture

A public website for sharing live and recorded video of the birds visiting one
backyard. Anyone can watch; only the owner publishes.

```
                         ┌──────────────────── DigitalOcean ─────────────────────┐
                         │                                                        │
  visitor's browser      │   Droplet (Debian 13 + Docker)                         │
  ─────────────────      │   ┌──────────────────────────────────────────────┐     │
   https://<domain>/ ────┼──▶│ caddy :80/:443  (TLS via Let's Encrypt)      │     │
        │                │   │    └─ reverse_proxy ─▶ web :8000 (FastAPI)   │     │
        │                │   │                         ├─ /            page  │     │
        │                │   │                         ├─ /api/config        │     │
        │                │   │                         ├─ /api/videos ◀──────┼──┐  │
        │                │   │                         ├─ /api/live   (stub) │  │  │
        │                │   │                         └─ /api/health        │  │  │
        │                │   │  [future] live ingest (e.g. MediaMTX)         │  │  │
        │                │   └──────────────────────────────────────────────┘  │  │
        │                │                                                     │  │
        │  <video src>   │   Spaces bucket  ──▶  Spaces CDN (edge cache)       │  │
        └────────────────┼──▶  videos/2026/11/<slug>.mp4                       │  │
                         │     posters/2026/11/<slug>.jpg                      │  │
                         │     catalog.json  ─────────────────────────────────────┘
                         │                                                        │
                         │   (no registry: image built on the droplet over SSH)   │
                         └────────────────────────────────────────────────────────┘
                                         ▲
  owner's workstation (Windows)          │  deploy-droplet deploy        (site code)
  ───────────────────────────────────────┘  python -m tools.publish ...  (videos)
```

## Components

| Piece | What it is | Where it lives |
|---|---|---|
| `webroot/` | The front page: plain HTML/CSS/JS, no build step, no framework | Baked into the image |
| `app/birdheaven/` | Small FastAPI service: serves the page, the catalog API, the live-status stub, security headers | Container on the droplet |
| Caddy | TLS termination, HTTP→HTTPS redirect, compression | Container on the droplet |
| Spaces + CDN | Every recorded video, its poster image, and `catalog.json` | DigitalOcean Spaces |
| `deploy/` | Workstation CLI: bootstrap droplet, build image on the droplet over SSH, `compose up`, roll back, prune | Your machine only |
| `tools/publish.py` | Workstation CLI: optimise a clip, cut a poster, upload both, add it to the catalog | Your machine only |

The deployment mechanics are lifted directly from `../financesite`: the
image is built on the droplet's own Docker daemon via `DOCKER_HOST=ssh://` and
started with `docker compose -f app/compose.yaml -f deploy/compose.prod.yaml up -d`.
Nothing is rsynced.

One deliberate departure: **no container registry.** The DigitalOcean account
allows one registry and financesite uses it. Since the build already runs on
the droplet's daemon, the image simply stays in that daemon's local store as
`birdheaven-web:<git sha>`, and compose runs it with `pull_policy: never`.
Old tags remain on the droplet for rollback (`deploy-droplet images`,
`deploy-droplet up --tag <sha>`) until `deploy-droplet prune` removes them.
The trade-off: if the droplet is rebuilt, there is no stored image to pull —
you just run `deploy` again, which rebuilds from source. Secrets and per-machine settings live in a gitignored
`deploy/.env.prod`, optionally as `op://` 1Password references.

What was dropped from financesite because this site doesn't need it:
Postgres, login/session auth, WebAuthn, Plaid and its webhook port. The site
is public and read-only, so the running container holds **no secrets at all**.

## Storage strategy: DigitalOcean Spaces with the built-in CDN

**Decision:** recorded video is stored as objects in a Spaces bucket and served
to browsers straight from the Spaces CDN endpoint. The droplet never touches
video bytes.

Why, against the alternatives:

| Option | Verdict |
|---|---|
| **Spaces + CDN** | **Chosen.** $5/mo base covers 250 GiB stored + 1 TiB egress, then ~$0.02/GiB stored and ~$0.01/GiB out. CDN is included at no extra cost. Videos scale independently of the droplet; a viral cardinal clip hits the CDN, not your 1 vCPU box. S3-compatible, so standard tooling (boto3, rclone, s3cmd) works. |
| Block-storage Volume on the droplet | Rejected. $0.10/GiB/mo (5× Spaces), every view streams through the droplet's CPU and bandwidth allowance, no edge cache, and a droplet outage takes the archive down with it. |
| Droplet root disk | Rejected. Small, tied to the droplet's lifecycle, same bandwidth problem. |
| YouTube / Vimeo embeds | Rejected for now. Free, but it's their player, their ads and their recommendations next to your birds. Easy to add later as a secondary mirror. |
| Managed video platform (Mux, Cloudflare Stream) | Overkill for a backyard archive; per-minute pricing grows with the archive. Worth revisiting only if adaptive bitrate becomes essential. |

**File format:** H.264 video + AAC audio in MP4, with the `moov` atom moved to
the front (`-movflags +faststart`) so playback starts before the download
finishes and seeking works via HTTP range requests. Every browser plays it
natively; no player library is needed for recordings. A 1080p clip at CRF 23 is
roughly 3–6 MB per minute of backyard footage. `tools/publish.py` handles
this. If clips get long (> ~10 min), HLS packaging is the next step and the
bucket layout already has room for it (`hls/<slug>/…`).

**Bucket layout:**

```
<bucket>/
  catalog.json                    ← the index of every published clip
  videos/YYYY/MM/<slug>.mp4       ← public-read, Cache-Control: 1 year, immutable
  posters/YYYY/MM/<slug>.jpg      ← public-read, Cache-Control: 1 year, immutable
```

Media objects are immutable (a re-edit gets a new slug), so they can be cached
forever at the edge. `catalog.json` changes on every publish and is uploaded
with `Cache-Control: max-age=60`.

**The catalog** is a JSON document, not a database. One owner publishing a
handful of clips a week does not justify running Postgres. Its shape:

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
      "width": 1920, "height": 1080, "bytes": 3812455
    }
  ]
}
```

Paths are relative to `MEDIA_BASE_URL` (the CDN endpoint, or a custom CDN
domain). The FastAPI service fetches the catalog server-side, caches it for 60
s, validates it, and serves it as `/api/videos` with absolute URLs. Proxying
the catalog — rather than having the browser fetch it from the CDN — avoids a
CORS configuration on the bucket and means a malformed catalog can't break the
page. The `<video>` and `<img>` elements load media from the CDN directly, which
needs no CORS.

**Cost guardrails:** the donate button exists because egress is the one
variable cost. Spaces' 1 TiB/month included transfer is ~250,000 one-minute
clip views; beyond that, a DigitalOcean billing alert is the backstop (set
one in the control panel — see `deploy/README.md`).

## Live video: a stub with a clear seam

The protocol isn't decided, so the site treats live video as configuration:

- `GET /api/live` returns `{"status": "offline" | "live", "kind": ..., "url": ..., "title": ...}`.
- Today it reflects environment variables (`LIVE_STATUS`, `LIVE_KIND`,
  `LIVE_URL`, `LIVE_TITLE`). Defaults: offline.
- The front-end player understands `kind`:
  - `hls` — plays via native HLS (Safari/iOS) or hls.js (loaded only when needed).
  - `video` — any URL a `<video>` element can play directly.
  - `iframe` — an embed URL (YouTube Live, Owncast's embed page, etc.).
  - anything else / offline — the "resting" card with the latest highlight.

The likely real implementations, all of which slot behind that endpoint
without changing the page:

| Path | Camera → | Browser gets | Notes |
|---|---|---|---|
| **MediaMTX** on the droplet | RTSP/RTMP/SRT push | HLS (and/or WebRTC via WHEP) | Single Go binary, one more compose service. Most flexible. |
| **Owncast** on the droplet | RTMP push from OBS | HLS + chat | Batteries-included; heavier. |
| **YouTube Live** | RTMP push | iframe | Zero server cost; their platform. |

When one is chosen: add the service to `deploy/compose.prod.yaml`, add a Caddy
route (e.g. `handle_path /live/* { reverse_proxy mediamtx:8888 }`), set
`LIVE_KIND=hls` and `LIVE_URL=/live/feeder/index.m3u8`, and replace the env-var
logic in `app/birdheaven/live.py` with a real probe of the ingest server so
`status` flips automatically when the camera connects. Ingest bandwidth (a
1080p camera pushes ~4–6 Mbps continuously) is the main reason to keep the
droplet and the live server sized together.

## Donations

A PayPal **hosted Donate button**, rendered as a plain HTML `<form>` that posts
to `https://www.paypal.com/donate`. No PayPal JavaScript runs on the page, which
keeps the Content-Security-Policy tight (`form-action` allows PayPal; nothing
else does). The button ID comes from `PAYPAL_HOSTED_BUTTON_ID`; the page hides
the support panel entirely if it isn't set. Create the button at
paypal.com → *Pay & Get Paid* → *Accept Donations* → *Donate Button*.

## Security posture

- Public, read-only site: no login, no user input stored, no database.
- Container holds no credentials. Spaces keys exist only on the workstation,
  used by `tools/publish.py`.
- Distroless, non-root runtime image (same base and reasoning as financesite).
- Strict CSP: scripts from self (+ cdnjs only for hls.js), media and images
  from self + the configured media origin, forms to self + PayPal,
  `frame-ancestors 'none'`.
- Caddy is the only published container; the app is reachable only on the
  compose network.

## Hosting note: separate droplet vs. sharing financesite's

Both stacks publish Caddy on :80/:443, so they cannot simply run side by side
on one droplet. The default here is a **separate droplet** (the cheapest $6
droplet is plenty, because video goes via the CDN). To co-host instead, drop
`caddy` from `deploy/compose.prod.yaml`, attach `web` to the finance stack's
network, and add a `birds.<domain> { reverse_proxy birdheaven-web-1:8000 }`
block to financesite's Caddyfile.

## Repository layout

```
ARCHITECTURE.md        this file
README.md              quick start
deploy-droplet.bat     cmd.exe launcher for python -m deploy
app/
  Dockerfile           distroless runtime, build context = repo root
  compose.yaml         local dev (web only, port 8000)
  requirements.txt
  birdheaven/          FastAPI service (main, config, catalog, live)
  tests/               pytest-free smoke tests (python -m app.tests.smoke)
webroot/               the front page
deploy/                droplet deployment CLI (adapted from financesite)
tools/publish.py       optimise + upload a clip, update catalog.json
dev/catalog.json       sample catalog for local development
```
