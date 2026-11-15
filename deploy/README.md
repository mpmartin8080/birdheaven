# Deploying Bird Heaven

The same workstation-driven flow as `../financesite/deploy`: run from
**Windows cmd.exe** via `deploy-droplet <command>` (a wrapper around
`py -3 -m deploy`). Both images, `birdheaven-web` (the public site) and
`birdheaven-admin` (the admin console), are built on the droplet's Docker
daemon over SSH and started with `docker compose` against that daemon. There
is **no container registry**: images stay in the droplet's local image store,
tagged with the git short SHA. See [ARCHITECTURE.md](../ARCHITECTURE.md) for
the overall design.

## One-time setup

1. **Droplet.** Debian 13; the $6 Basic size is enough. Add your SSH key. Use a
   separate droplet from financesite; both stacks need ports 80 and 443.
2. **DNS.** One A record for the site (e.g. `birdheaven.example.net`) pointing
   at the droplet's IP. The admin console uses the same hostname on port 7777
   (`https://birdheaven.example.net:7777/`), so it needs no record of its own.
3. **Spaces bucket.** Control panel → Spaces → Create bucket. Then:
   - Turn on **CDN** for the bucket.
   - Leave **file listing restricted**. Objects are made public one at a time.
   - API → **Spaces Keys** → generate a key, scoped to this bucket if offered.
4. **Firewall.** A DigitalOcean Cloud Firewall with these inbound rules:

   | Port | Sources | Why |
   |---|---|---|
   | 22 | your IP | SSH for the deploy tool |
   | 80, 443 | **All IPv4 + All IPv6** | the public site, and Let's Encrypt validation |
   | 7777 | your IP (recommended), or everyone | the admin console |

   Let's Encrypt never connects to 7777; the certificate is validated over
   80/443 and simply reused there. So restricting 7777 to the addresses you
   manage the site from is free extra protection on top of passkeys. If your
   home IP changes, update the rule. Docker-published ports bypass ufw, so the
   Cloud Firewall is the one that actually protects them.
5. **Billing alert.** Control panel → Billing → set an alert (e.g. $15).
6. **Workstation tools.** `py -3 -m pip install -r deploy\requirements.txt`
   (argon2-cffi, for hashing the recovery password).
7. **Settings.**
   ```
   copy deploy\.env.example deploy\.env.prod
   notepad deploy\.env.prod          (host, domain, CATALOG_URL, SPACES_* keys, SMTP_*)
   deploy-droplet admin-password --enable
   deploy-droplet check
   ```
   `admin-password` asks for a recovery password (12+ characters), writes its
   Argon2 hash and a fresh `ADMIN_SESSION_KEY` into `.env.prod`, and with
   `--enable` turns on `ADMIN_PASSWORD_LOGIN` for the first sign-in.
8. **Bootstrap the droplet** (installs Docker, opens ufw 22/80/443/7777):
   ```
   deploy-droplet bootstrap
   ```
   It's safe to re-run, and you need to re-run it if you change
   `BIRD_DEPLOY_ADMIN_PORT` or `BIRD_DEPLOY_HTTPS_PORT`.

## First sign-in to the admin console

1. `deploy-droplet deploy`. It prints both URLs and checks both certificates.
2. Open `https://<your domain>:7777/` and sign in with the recovery password.
   A yellow banner reminds you that password sign-in is on.
3. **Passkeys** tab → name it (e.g. "iPhone") → **Add a passkey** → follow the
   device prompt. Add one on each device you manage the site from (a phone and
   a laptop is a good pair).
4. Turn the password off: set `ADMIN_PASSWORD_LOGIN=false` in
   `deploy\.env.prod`, then run `deploy-droplet up`. This also ends the
   password session. Sign in again with your passkey.

**Lost every passkey (or a new droplet)?** Same procedure: set
`ADMIN_PASSWORD_LOGIN=true`, run `deploy-droplet up`, sign in with the
recovery password, register a passkey, switch it back off, and run
`deploy-droplet up`. Forgotten the recovery password too? Run
`deploy-droplet admin-password --enable` to set a new one, then
`deploy-droplet up`.

The console won't let you remove your last passkey while password sign-in is
off; that would lock you out.

## Releasing

```
deploy-droplet deploy          build both images on the droplet + up (tag = git short SHA)
deploy-droplet up              re-apply .env.prod changes (e.g. the password switch) to the running images
deploy-droplet status          SSH, containers, certificates, both /api/health endpoints
deploy-droplet logs -f
deploy-droplet images          tags on the droplet, newest first
deploy-droplet up --tag abc1234    roll back to an earlier pair of images
deploy-droplet prune --keep 5  delete older tags and stale build cache
```

During first bring-up, if DNS or the certificate isn't working yet,
`BIRD_DEPLOY_PORT80_MODE=passthrough` serves the *public* site over plain HTTP.
The admin console is always HTTPS-only.

## TLS

Same mechanism as financesite: **Caddy's automatic HTTPS** with Let's Encrypt.
Naming the site's hostname in `deploy/caddy/Caddyfile` is the whole request.
Caddy obtains **one** certificate on start, over HTTP-01 on `:80` (falling
back to TLS-ALPN on `:443`), stores it in the `caddy-data` volume, renews it
automatically, and serves it on both `:443` and the admin port `:7777`. A
certificate is issued for a hostname, never a port, which is also why 7777
needs no exposure to Let's Encrypt.

```
deploy-droplet cert              get the certificate and verify it on both ports (waits up to 180 s)
deploy-droplet cert --wait 600
```

`cert` checks that the hostname resolves to the droplet and that Caddy is
running, restarts Caddy so issuance starts now, and waits until `:443` serves
a publicly trusted certificate, showing the issuer and expiry. It then checks
that `:7777` serves the same certificate; if it can't connect there, the
cause is the firewall, not Let's Encrypt, and it says so. On failure it prints
Caddy's ACME log and names the known failure modes: firewall, rate limit, DNS.
`deploy` runs the same check without failing, and `status` shows both ports.

**Requirements Let's Encrypt imposes:**

- The A record must point at the droplet.
- **Ports 80 and 443 must be open to the whole internet**, not just your IP. A
  firewall restricted to your own IP is the classic failure: the site works
  from home while Caddy logs `Timeout during connect (likely firewall
  problem)`.
- After repeated failures Let's Encrypt rate-limits the hostname (5 failed
  validations per hour). Fix the cause, then run `cert` again.

## Publishing videos

Normally: **the admin console's Upload tab.** Drop a file in, give it a title
and species, and it is processed and published in the background. H.264
clips from most cameras and phones take seconds; other formats are converted
on the droplet, which is slower.

For batches or scripting, `tools/publish.py` does the same thing from your
workstation, using the same code:

```
py -3 -m pip install -r tools\requirements.txt
winget install Gyan.FFmpeg

py -3 -m tools.publish add D:\clips\cardinal.mov --title "Cardinal pair at the platform feeder" ^
    --species "Northern Cardinal" --description "The male hands off a seed." --featured
py -3 -m tools.publish list
py -3 -m tools.publish remove 2026-11-14-cardinal-pair
```

Changes reach the public site within about a minute (60 s cache in the app,
plus up to 30 s at the CDN). Pointing `CATALOG_URL` at the bucket's origin
endpoint instead of the CDN removes the CDN part.

## Contact form email

The front page's contact form sends to `CONTACT_EMAIL` in `deploy\.env.prod`
(default `bird.heaven.admin@gmail.com`). Visitors never see that address, and
the sender's address is set as Reply-To.

Until `SMTP_HOST` is set, messages are only written to the web container's
log. Read them with `deploy-droplet logs web`. To get real email:

1. **DigitalOcean blocks outbound SMTP** (ports 25/465/587) on new droplets.
   Pick one:
   - **Ask DO support to lift the block.** Then use Gmail: `SMTP_HOST=smtp.gmail.com`,
     `SMTP_PORT=587`, `SMTP_USERNAME=` the Gmail address, `SMTP_PASSWORD=` a
     Google **app password** (Google account → Security → 2-Step Verification
     → App passwords).
   - **Use a relay on port 2525** (SMTP2GO, Mailgun, SendGrid, Brevo, ...):
     their host, `SMTP_PORT=2525`, their credentials, and a `CONTACT_FROM`
     address you have verified with them.
2. Run `deploy-droplet up`, then send yourself a test message from the site.
   If it fails, `deploy-droplet logs web` shows the SMTP error.

## Access logs

The admin console's **Logs** tab shows every HTTP request to the public site
and to the console. Caddy writes these logs to the `caddy-logs` volume as JSON
and rotates them at 10 MiB, keeping 5 files per site. In local development
there is no Caddy, so the tab says there are no logs.

## Changing site settings

Use the admin console's **Site settings** tab: name, tagline, intro, location,
PayPal button, and live stream. No deploy needed. The matching values in
`deploy/.env.prod` are only defaults for anything the console leaves blank.
