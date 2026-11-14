"""Obtain and verify the site's TLS certificate -- financesite's mechanism, driven.

The mechanism is exactly financesite's: **Caddy's automatic HTTPS**. Naming
`{$SITE_DOMAIN}:{$HTTPS_PORT}` as a site in deploy/caddy/Caddyfile is the whole
request. On start, Caddy registers an ACME account with Let's Encrypt, answers
the HTTP-01 challenge on plain :80 (the `http://{$SITE_DOMAIN}` block owns that
port, and Caddy layers the challenge responder underneath it in either
PORT80_MODE), stores the certificate in its /data volume (`caddy-data`, see
deploy/compose.prod.yaml), and renews it on its own about 30 days before
expiry. There is no certbot, no cron job and no certificate file to manage.

So "creating" the certificate means: make sure Let's Encrypt can reach the
droplet for this hostname, make sure Caddy is running with the current
Caddyfile, and wait until the hostname really serves a publicly trusted
certificate. That is what `ensure()` does, and it explains what is wrong when
issuance doesn't happen, instead of leaving you to dig through Caddy's logs.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
from datetime import datetime, timezone

from . import compose_ops
from .config import DeployConfig

# Lines in Caddy's log worth showing when issuance stalls.
_LOG_KEYWORDS = ("acme", "certificate", "challenge", "obtain", "tls", "error", "rate")


def _resolve(domain: str) -> set[str]:
    try:
        return {info[4][0] for info in socket.getaddrinfo(domain, None, socket.AF_INET)}
    except socket.gaierror:
        return set()


def check_dns(cfg: DeployConfig, domain: str) -> bool:
    """True when the domain's A record(s) point at the droplet."""
    addrs = _resolve(domain)
    print(f"==> DNS: {domain} -> {', '.join(sorted(addrs)) or 'no A record'}")
    if not addrs:
        print(f"  Let's Encrypt cannot validate a name that doesn't resolve. Add an A "
              f"record for {domain} pointing at {cfg.host}.")
        return False
    if cfg.host not in addrs:
        print(f"  The droplet is {cfg.host}, which is not among those addresses. "
              f"Let's Encrypt would send its challenge elsewhere. Fix the A record "
              f"(or wait for the old one's TTL to expire).")
        return False
    print("  ok")
    return True


def check_port80(cfg: DeployConfig) -> bool:
    """The HTTP-01 challenge always arrives on :80, whatever the HTTPS port."""
    print(f"==> Port 80 on {cfg.host} (the ACME HTTP-01 challenge)")
    try:
        with socket.create_connection((cfg.host, 80), timeout=8):
            # Only proves *this workstation* gets through. A Cloud Firewall that
            # allows your own IP but not "All IPv4/All IPv6" passes this check and
            # still blocks Let's Encrypt -- diagnose() catches that from Caddy's log.
            print("  open from this workstation (Let's Encrypt connects from its own, "
                  "unpublished addresses -- the Caddy log below is the real test)")
            return True
    except OSError as err:
        print(f"  not reachable ({err}). Open 80/tcp in the DigitalOcean Cloud "
              f"Firewall; `deploy-droplet bootstrap` already opens it in ufw.")
        return False


def fetch_cert(cfg: DeployConfig, domain: str, port: int | None = None) -> dict | None:
    """The certificate the droplet presents for `domain` on `port` (default: the
    site's HTTPS port), if publicly trusted; else None."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((cfg.host, port or cfg.https_port), timeout=8) as sock:
            # Connect to the droplet itself, but validate as the hostname, so
            # this checks *this* server even if DNS hasn't propagated locally.
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                return tls.getpeercert()
    except (ssl.SSLError, ssl.CertificateError, OSError):
        return None


def describe(cert: dict) -> str:
    issuer = dict(x[0] for x in cert.get("issuer", ()))
    not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
    days = (not_after - datetime.now(timezone.utc)).days
    names = ", ".join(v for k, v in cert.get("subjectAltName", ()) if k == "DNS")
    return (f"  names:   {names}\n"
            f"  issuer:  {issuer.get('organizationName', '?')} ({issuer.get('commonName', '?')})\n"
            f"  expires: {not_after:%Y-%m-%d} ({days} days; Caddy renews automatically)")


def _log_entries(cfg: DeployConfig) -> list[dict]:
    """Caddy's JSON log lines, parsed; `compose logs` prefixes each with 'caddy-1  | '."""
    entries = []
    for line in compose_ops.service_logs(cfg, "caddy", tail=400).splitlines():
        _, _, body = line.partition("| ")
        try:
            entry = json.loads(body or line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _show_caddy_log(entries: list[dict]) -> None:
    print("==> Recent Caddy certificate activity")
    shown = 0
    for e in entries[-60:]:
        text = json.dumps(e).lower()
        if not any(k in text for k in _LOG_KEYWORDS):
            continue
        when = datetime.fromtimestamp(e.get("ts", 0), timezone.utc).strftime("%H:%M:%S")
        detail = (e.get("problem") or {}).get("detail") or e.get("error") or ""
        print(f"  {when} {e.get('level', ''):5} {e.get('msg', '')}"
              + (f" -- {str(detail)[:220]}" if detail else ""))
        shown += 1
    if not shown:
        print("  (nothing relevant logged yet)")


# Known ACME failures, recognised from Caddy's log, with the fix for each.
_DIAGNOSES = (
    (("timeout during connect", "likely firewall problem"),
     "Let's Encrypt cannot connect to the droplet on :80/:443 (\"Timeout during "
     "connect\"), even though this workstation can. That is almost always a "
     "DigitalOcean Cloud Firewall whose inbound HTTP/HTTPS rules allow only "
     "specific sources (e.g. your own IP). Let's Encrypt validates from many "
     "unpublished addresses, so in the control panel (Networking > Firewalls > "
     "the firewall attached to this droplet > Inbound Rules) set the HTTP (80) "
     "rule's sources -- and HTTPS (443) for a public site -- to \"All IPv4\" and "
     "\"All IPv6\". Then re-run `deploy-droplet cert`."),
    (("ratelimited", "too many failed authorizations", "too many certificates"),
     "Let's Encrypt is rate-limiting this hostname after repeated failures. Fix the "
     "underlying problem, then wait (failed-validation limits reset within the "
     "hour); Caddy retries on its own and falls back to the staging CA meanwhile."),
    (("nxdomain", "dns problem", "no valid a records"),
     "Let's Encrypt's resolvers cannot see the A record yet. If you just created "
     "it, wait for DNS to propagate; otherwise check the record at your DNS host."),
    (("connection refused",),
     "Something reached the droplet but nothing answered on :80. Check that the "
     "caddy container is running and publishing port 80 (`deploy-droplet ps`)."),
)


def diagnose(entries: list[dict]) -> str | None:
    """The explanation for the most recent recognised ACME failure, if any."""
    for e in reversed(entries[-80:]):
        if e.get("level") != "error":
            continue
        text = json.dumps(e).lower()
        for needles, advice in _DIAGNOSES:
            if any(n in text for n in needles):
                return advice
    return None


def _check_admin_port(cfg: DeployConfig, fail) -> bool:
    """The admin port serves the same certificate -- nothing to issue, only to reach.

    So a failure here is never Let's Encrypt's: it means this workstation can't
    connect to the admin port (Cloud Firewall) or Caddy isn't listening on it.
    """
    print(f"==> Admin console port {cfg.admin_port} (same certificate)")
    if fetch_cert(cfg, cfg.site_domain, cfg.admin_port):
        print(f"  ok -- {cfg.admin_origin}/")
        return True
    return fail(
        f"the certificate is fine, but {cfg.admin_origin} did not answer from this "
        f"workstation. Allow inbound TCP {cfg.admin_port} in the DigitalOcean Cloud "
        f"Firewall (your own IP is enough), re-run `deploy-droplet bootstrap` so ufw "
        f"allows it too, and check `deploy-droplet ps` shows caddy publishing it."
    )


def ensure(cfg: DeployConfig, *, wait: int = 180, strict: bool = True) -> bool:
    """Make sure the site serves a trusted certificate, waiting for Caddy to get one.

    strict=False (used at the end of `deploy`) reports problems without failing,
    since the site itself is already up and issuance may simply need DNS time.
    """
    def fail(msg: str) -> bool:
        if strict:
            raise SystemExit(msg)
        print(f"  warning: {msg}")
        return False

    domain = cfg.site_domain
    print(f"==> TLS certificate for {domain} (port {cfg.https_port})")
    cert = fetch_cert(cfg, domain)
    if cert:
        print("  already serving a trusted certificate")
        print(describe(cert))
        return _check_admin_port(cfg, fail)
    print("  none yet")

    dns_ok = check_dns(cfg, domain)
    port_ok = check_port80(cfg)
    if not compose_ops.service_running(cfg, "caddy"):
        return fail("Caddy is not running on the droplet. Run `deploy-droplet deploy` "
                    "first -- Caddy requests the certificate when it starts.")
    if not (dns_ok and port_ok):
        return fail("fix the problem(s) above, then run `deploy-droplet cert` again. "
                    "Caddy keeps retrying on its own with backoff, so once they are "
                    "fixed the certificate will arrive.")

    previous = diagnose(_log_entries(cfg))
    if previous:
        print("==> Caddy's earlier attempts failed:")
        print(f"  {previous}")
        print("  Retrying anyway, in case that has been fixed since.")

    # Restart so issuance is attempted now with the current Caddyfile, instead of
    # waiting out Caddy's retry backoff from an earlier failed attempt.
    print("==> Restarting Caddy to start issuance now")
    compose_ops.restart_service(cfg, "caddy")

    print(f"==> Waiting up to {wait}s for Let's Encrypt", end="", flush=True)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(5)
        print(".", end="", flush=True)
        cert = fetch_cert(cfg, domain)
        if cert:
            print(f"\n  issued for {domain}")
            print(describe(cert))
            return _check_admin_port(cfg, fail)
    print()
    entries = _log_entries(cfg)
    _show_caddy_log(entries)
    advice = diagnose(entries)
    if advice:
        return fail(f"no trusted certificate after {wait}s.\n\n  {advice}")
    return fail(
        f"no trusted certificate after {wait}s. Check the log lines above. "
        f"Common causes: DNS not yet propagated to Let's Encrypt's resolvers, :80 "
        f"blocked upstream, or a Let's Encrypt rate limit (5 failed validations "
        f"per hostname per hour). Caddy keeps retrying by itself; re-run "
        f"`deploy-droplet cert` to check again."
    )
