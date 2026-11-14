# Deploying Bird Heaven

The same workstation-driven flow as `../financesite/deploy`: run from
**Windows cmd.exe** via `deploy-droplet <command>` (a wrapper around
`py -3 -m deploy`). The image is built on the droplet's Docker daemon over
SSH and started with `docker compose` against that daemon. There is **no
container registry**: the image stays in the droplet's local image store,
tagged with the git short SHA. Only the build context (a few hundred KB) crosses
the network on each deploy. See [ARCHITECTURE.md](../ARCHITECTURE.md) for
the overall design.

## One-time setup

1. **Droplet.** Create a Debian 13 droplet (the $6 Basic size is enough,
   because video is served by the Spaces CDN, not the droplet). Add your SSH key.
   Use a separate droplet from financesite; both stacks need ports 80 and 443.
2. **DNS.** Point an A record (e.g. `birds.example.net`) at the droplet's IP.
3. **Spaces bucket.** Control panel → Spaces → Create bucket (e.g.
   `birdheaven`, region `nyc3`). Then:
   - Turn on **CDN** for the bucket. Note the endpoint,
     `https://birdheaven.nyc3.cdn.digitaloceanspaces.com`.
   - Leave **file listing restricted**. Objects are made public one by one by
     `tools/publish.py`, so the bucket index itself stays private.
   - API → **Spaces Keys** → generate a key, scoped to this bucket if offered.
4. **Billing alert.** Control panel → Billing → set an alert (e.g. $15). Egress
   is the only cost that grows with traffic.
5. **Settings.**
   ```
   copy deploy\.env.example deploy\.env.prod
   notepad deploy\.env.prod          (host, domain, CATALOG_URL, PayPal id, keys)
   deploy-droplet check
   ```
6. **Bootstrap the droplet** (installs Docker, opens ufw 22/80/443):
   ```
   deploy-droplet bootstrap
   ```
   Also add a DigitalOcean Cloud Firewall allowing 22, 80 and 443 inbound.
   Docker-published ports bypass ufw, so the Cloud Firewall is the one that
   actually protects them.

## Releasing

```
deploy-droplet deploy          build on the droplet + up (tag = git short SHA)
deploy-droplet status          SSH, containers, https://<domain>/api/health
deploy-droplet logs -f
deploy-droplet images          tags on the droplet, newest first
deploy-droplet up --tag abc1234    roll back to an earlier image (it becomes `latest`)
deploy-droplet prune --keep 5  delete older tags and stale build cache
```

Each deploy leaves its image on the droplet (roughly 150 MB each, with most
layers shared between them). Run `prune` now and then. It never removes the
image that `latest` points at, which is always the one that was last started.

During first bring-up, if DNS or the certificate isn't working yet, set
`BIRD_DEPLOY_PORT80_MODE=passthrough` to reach the site over plain HTTP, then
set it back to `redirect`.

## Publishing videos

Video does **not** go through a deploy. It goes straight to Spaces:

```
py -3 -m pip install -r tools\requirements.txt
winget install Gyan.FFmpeg

py -3 -m tools.publish add D:\clips\cardinal.mov --title "Cardinal pair at the platform feeder" ^
    --species "Northern Cardinal" --description "The male hands off a seed." --featured
py -3 -m tools.publish list
py -3 -m tools.publish remove 2026-11-14-cardinal-pair --delete-files
```

The site picks up catalog changes within about a minute (60 s catalog cache in
the app, plus `max-age=60` on `catalog.json` at the CDN). Use
`--dry-run publish-out` to check the encode and poster frame before
uploading anything.

## Changing site settings

PayPal ID, location label, live stream fields: edit `deploy/.env.prod` and
run `deploy-droplet up`. No rebuild is needed.
