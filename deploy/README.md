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
2. **DNS.** An A record pointing at the droplet's IP for every hostname in
   `BIRD_DEPLOY_SITE_DOMAIN` (e.g. `birdheaven.example.net` and
   `www.birdheaven.example.net`). The admin console uses the first hostname on
   port 7777 (`https://birdheaven.example.net:7777/`), so it needs no record of
   its own.
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
   notepad deploy\.env.prod          (host, domain(s), ACME email, CATALOG_URL, SPACES_* keys, SMTP_*)
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

**One Let's Encrypt certificate with every hostname as a SAN.** List the
names in `BIRD_DEPLOY_SITE_DOMAIN`, comma-separated, primary first. The
others redirect to the primary, on HTTP and HTTPS. Caddy's own automatic
HTTPS (financesite's mechanism) can only issue one certificate per name, so a
small `certs` service does the issuing:

- It runs **lego**, an ACME client, and requests the certificate over HTTP-01.
  It writes each challenge file into a shared volume, which Caddy serves on
  `:80` for every hostname.
- It installs the result in the `certs` volume. Caddy loads it on `:443` and
  on the admin port `:7777`, and reloads itself within 30 s whenever the file
  changes. No restart is needed.
- It checks twice a day and renews within 30 days of expiry. On failure it
  backs off from 15 minutes to 6 hours, well inside Let's Encrypt's limits.
- On a brand-new droplet it first installs a 7-day self-signed placeholder,
  so Caddy can start. Until the real certificate arrives, browsers warn.
- If you change the hostname list and run `deploy-droplet up`, a new
  certificate covering the new list is requested automatically.

A certificate is issued for hostnames, never ports, which is why 7777 needs no
exposure to Let's Encrypt.

```
deploy-droplet cert              get the certificate and verify it everywhere (waits up to 180 s)
deploy-droplet cert --wait 600
```

`cert` runs these checks, in order:

1. Every hostname resolves to the droplet.
2. `:80` is open.
3. The challenge path works end to end for each name. It drops a test token
   into the webroot and fetches `http://<name>/.well-known/acme-challenge/<token>`,
   exactly as Let's Encrypt will.
4. It restarts the certs service so issuance starts now.
5. It waits until a publicly trusted certificate covering **all** the names
   is served, and shows its names, issuer and expiry.
6. It checks each alias and `:7777`. If it can't connect to `:7777`, the cause
   is the firewall, not Let's Encrypt, and it says so.

On failure it prints the certs log and names the known failure modes:
firewall, rate limit, DNS, a name pointing elsewhere. `deploy` runs the same
check without failing, and `status` shows every hostname and port.

`BIRD_DEPLOY_ACME_EMAIL` is required: the ACME client won't register an
account without it. For a trial run that can't hit production rate limits,
set `BIRD_DEPLOY_ACME_SERVER` to Let's Encrypt staging (see
`.env.example`). Remove it again for a trusted certificate.

**Requirements Let's Encrypt imposes:**

- Every hostname's A record must point at the droplet.
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
   If it fails, `deploy-droplet logs web` shows the SMTP error, what it
   usually means, and the undelivered message, so nothing is lost. A timeout
   or `Network is unreachable` means the port is blocked; that happens before
   the password is ever tried.

## Access logs

The admin console's **Logs** tab shows every HTTP request to the public site
and to the console. Caddy writes these logs to the `caddy-logs` volume as JSON
and rotates them at 10 MiB, keeping 5 files per site. In local development
there is no Caddy, so the tab says there are no logs.

Its **Sign-ins** view is the console's own record of authentication. It
lists every sign-in, successful or failed (with the reason), throttled
attempts, password attempts made while the switch is off, sign-outs, and
passkeys added, renamed or removed. Each event shows its time, IP and
browser. Passwords are never recorded. The log is kept in
`auth-log.jsonl` on the admin's private volume, so it survives deploys,
and rolls over at 1 MiB.

## Changing site settings

Use the admin console's **Site settings** tab: name, tagline, intro, location,
PayPal button, and live stream. No deploy needed. The matching values in
`deploy/.env.prod` are only defaults for anything the console leaves blank.
