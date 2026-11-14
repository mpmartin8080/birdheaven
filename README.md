# Bird Heaven

Live and recorded video of the birds at our backyard feeders, as a public
website, with a passkey-protected admin console for managing clips and settings.

- **Design and decisions:** [ARCHITECTURE.md](ARCHITECTURE.md). It covers
  storage (Spaces + CDN), the admin console and its auth, the live-video stub,
  PayPal donations and security.
- **Deploying, first sign-in, recovery:** [deploy/README.md](deploy/README.md)

## Run it locally

Everything works locally without Spaces: `dev/media/` stands in for the bucket.

**With Docker** (closest to production):

```
cd app
docker compose up --build
```

- Public site: http://localhost:8000
- Admin console: http://localhost:8001. It needs `app/.env` first; see the
  header of [app/compose.yaml](app/compose.yaml). Passkeys work on
  `localhost` too.

**Without Docker:**

```
py -3 -m pip install fastapi uvicorn httpx -r admin\requirements.txt
cd app && py -3 -m uvicorn birdheaven.main:app --reload --port 8000
```

The admin console needs FFmpeg on PATH (`winget install Gyan.FFmpeg`) and
these environment variables:

```
ADMIN_SESSION_KEY=<48+ characters>  ADMIN_COOKIE_SECURE=false
ADMIN_PASSWORD_LOGIN=true  ADMIN_PASSWORD_HASH=<py -3 -m birdadmin.hashpw, in admin/>
ADMIN_LOCAL_STORAGE_DIR=..\dev\media  PUBLIC_SITE_URL=http://localhost:8000/
cd admin && py -3 -m uvicorn birdadmin.main:app --port 8001
```

The sample catalog has 8 clips with drawn posters but no real `.mp4` files.
Upload a real clip through the local admin console to see playback end to end.

## Tests

```
py -3 -m app.tests.smoke       public site
py -3 -m admin.tests.smoke     admin console (covers the upload pipeline when ffmpeg is on PATH)
```
