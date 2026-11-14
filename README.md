# Bird Heaven

Live and recorded video of the birds at our backyard feeders, as a public website.

- **Design and decisions:** [ARCHITECTURE.md](ARCHITECTURE.md). It covers the
  storage strategy (DigitalOcean Spaces + CDN), the live-video stub, PayPal
  donations, and security.
- **Deploying and publishing clips:** [deploy/README.md](deploy/README.md)

## Run it locally

```
py -3 -m pip install fastapi uvicorn httpx
cd app
py -3 -m uvicorn birdheaven.main:app --reload --port 8000
```

Open http://localhost:8000. With no `CATALOG_URL` set, the site reads
`dev/catalog.json`, a sample of 8 clips with generated SVG posters. Their
`.mp4` files don't exist, so opening one shows the player's error state. Drop a
real MP4 at `dev/media/videos/<slug>.mp4` to see playback.

To show the donate panel locally, set `PAYPAL_HOSTED_BUTTON_ID=anything`. To
try the live player, set `LIVE_STATUS=live LIVE_KIND=video LIVE_URL=<some mp4 URL>`.

Or run it in Docker, the way it runs in production:

```
cd app && docker compose up --build
```

## Tests

```
py -3 -m app.tests.smoke
```
