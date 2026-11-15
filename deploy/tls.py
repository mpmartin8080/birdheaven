"""Obtain and verify the site's TLS certificate: one Let's Encrypt certificate
with every hostname in BIRD_DEPLOY_SITE_DOMAIN as a SAN.

Who does what (see deploy/certs/certs.sh and deploy/caddy/Caddyfile):

  * The `certs` service runs lego. It requests the certificate over HTTP-01,
    writing each challenge file into the shared `acme-webroot` volume. It
    checks twice a day and renews within 30 days of expiry. Until the first real
    certificate exists it installs a self-signed placeholder so Caddy can start.
  * Caddy serves /.well-known/acme-challenge/ from that volume on :80 for every
    hostname, loads /certs/site.crt on the HTTPS and admin ports, and reloads
    itself when the file changes (deploy/caddy/run.sh).

Caddy's own automatic HTTPS (financesite's mechanism) can only issue one
certificate per hostname, which is why issuance moved to lego.

`ensure()` makes sure Let's Encrypt can reach every hostname, kicks the certs
service, and waits until the droplet serves a publicly trusted certificate
covering all the names -- explaining what is wrong when it doesn't, instead of
leaving you to dig through logs.
"""

from __future__ import annotations

import secrets
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import compose_ops
from .config import DeployConfig

# Lines in the certs service's log worth showing when issuance stalls.
_LOG_KEYWORDS = ("acme", "certificate", "challenge", "obtain", "error", "rate", "fail",
                 "install", "placeholder", "renew")


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
            # still blocks Let's Encrypt -- diagnose() catches that from the log.
            print("  open from this workstation (Let's Encrypt connects from its own, "
                  "unpublished addresses -- the certs log is the real test)")
            return True
    except OSError as err:
        print(f"  not reachable ({err}). Open 80/tcp in the DigitalOcean Cloud "
              f"Firewall; `deploy-droplet bootstrap` already opens it in ufw.")
        return False


def check_challenge_path(cfg: DeployConfig) -> bool:
    """Prove the HTTP-01 path works for every hostname, end to end.

    Puts a token file in the shared webroot (as lego does) and fetches it the
    way Let's Encrypt would: http://<each name>/.well-known/acme-challenge/<token>.
    That exercises DNS, :80, Caddy's routing and the shared volume in one go.
    """
    token = f"birdheaven-selftest-{secrets.token_hex(8)}"
    print("==> HTTP-01 challenge path, every hostname")
    if not compose_ops.exec_service(
            cfg, "certs",
            ["sh", "-c", f"mkdir -p /acme/.well-known/acme-challenge && "
                         f"echo {token} > /acme/.well-known/acme-challenge/{token}"]):
        print("  could not write a test token into the certs webroot (is certs running?)")
        return False
    ok = True
    try:
        for domain in cfg.site_domains:
            url = f"http://{domain}/.well-known/acme-challenge/{token}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "birdheaven-deploy"})
                with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                    body = resp.read(200).decode("utf-8", "replace").strip()
                good = body == token
            except (urllib.error.URLError, OSError) as err:
                body, good = str(err), False
            print(f"  {domain}: {'ok' if good else 'FAILED -- ' + body[:160]}")
            ok = ok and good
    finally:
        compose_ops.exec_service(cfg, "certs", ["rm", "-f", f"/acme/.well-known/acme-challenge/{token}"])
    if not ok:
        print("  Let's Encrypt fetches exactly these URLs. Check each failing name's A "
              "record, and that Caddy is running the current Caddyfile (`deploy-droplet up`).")
    return ok


def fetch_cert(cfg: DeployConfig, domain: str, port: int | None = None) -> dict | None:
    """The certificate the droplet presents for `domain` on `port` (default: the
    site's HTTPS port), if publicly trusted for that name; else None."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((cfg.host, port or cfg.https_port), timeout=8) as sock:
            # Connect to the droplet itself, but validate as the hostname, so
            # this checks *this* server even if DNS hasn't propagated locally.
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                return tls.getpeercert()
    except (ssl.SSLError, ssl.CertificateError, OSError):
        return None


def cert_names(cert: dict) -> set[str]:
    return {v.lower() for k, v in cert.get("subjectAltName", ()) if k == "DNS"}


def missing_names(cfg: DeployConfig, cert: dict) -> list[str]:
    """Configured hostnames the certificate does not cover (yet)."""
    have = cert_names(cert)
    return [d for d in cfg.site_domains if d not in have]


def describe(cert: dict) -> str:
    issuer = dict(x[0] for x in cert.get("issuer", ()))
    not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
    days = (not_after - datetime.now(timezone.utc)).days
    names = ", ".join(v for k, v in cert.get("subjectAltName", ()) if k == "DNS")
    return (f"  names:   {names}\n"
            f"  issuer:  {issuer.get('organizationName', '?')} ({issuer.get('commonName', '?')})\n"
            f"  expires: {not_after:%Y-%m-%d} ({days} days; the certs service renews it "
            f"automatically)")


def _log_lines(cfg: DeployConfig) -> list[str]:
    """The certs service's log (lego + certs.sh), without compose's 'certs-1  | ' prefix."""
    out = []
    for line in compose_ops.service_logs(cfg, "certs", tail=400).splitlines():
        _, sep, body = line.partition("| ")
        out.append((body if sep else line).rstrip())
    return [line for line in out if line]


def _show_log(lines: list[str]) -> None:
    print("==> Recent certificate activity (certs service)")
    shown = [line for line in lines[-80:] if any(k in line.lower() for k in _LOG_KEYWORDS)]
    for line in shown[-30:]:
        print(f"  {line[:300]}")
    if not shown:
        print("  (nothing relevant logged yet)")


# Known ACME failures, recognised from the log, with the fix for each.
_DIAGNOSES = (
    (("timeout during connect", "likely firewall problem"),
     "Let's Encrypt cannot connect to the droplet on :80 (\"Timeout during "
     "connect\"), even though this workstation can. That is almost always a "
     "DigitalOcean Cloud Firewall whose inbound HTTP rule allows only specific "
     "sources (e.g. your own IP). Let's Encrypt validates from many unpublished "
     "addresses, so in the control panel (Networking > Firewalls > the firewall "
     "attached to this droplet > Inbound Rules) set the HTTP (80) rule's sources "
     "-- and HTTPS (443) for a public site -- to \"All IPv4\" and \"All IPv6\". "
     "Then re-run `deploy-droplet cert`."),
    (("ratelimited", "rate limit", "too many failed authorizations", "too many certificates"),
     "Let's Encrypt is rate-limiting these hostnames after repeated failures or "
     "issuances. Fix the underlying problem, then wait (failed-validation limits "
     "reset within the hour). The certs service retries on its own, backing off."),
    (("nxdomain", "dns problem", "no valid a records", "no valid ip addresses"),
     "Let's Encrypt's resolvers cannot see an A record for one of the names yet. "
     "If you just created it, wait for DNS to propagate; otherwise check the "
     "record at your DNS host. Every name in BIRD_DEPLOY_SITE_DOMAIN needs one."),
    (("invalid response from", "unauthorized"),
     "Let's Encrypt reached a server for one of the names but didn't get the "
     "challenge file back. Usually that name's A record points somewhere other "
     "than this droplet, or Caddy is running an older Caddyfile -- run "
     "`deploy-droplet up`, then `deploy-droplet cert` again."),
    (("connection refused",),
     "Something reached the droplet but nothing answered on :80. Check that the "
     "caddy container is running and publishing port 80 (`deploy-droplet ps`)."),
)


def diagnose(lines: list[str]) -> str | None:
    """The explanation for the most recent recognised ACME failure, if any."""
    for line in reversed(lines[-120:]):
        text = line.lower()
        if "error" not in text and "fail" not in text:
            continue
        for needles, advice in _DIAGNOSES:
            if any(n in text for n in needles):
                return advice
    return None


def _check_other_listeners(cfg: DeployConfig, fail) -> bool:
    """Aliases and the admin port serve the same certificate: nothing to issue,
    only to reach. A failure here is a routing/firewall problem, never ACME."""
    ok = True
    for domain in cfg.alias_domains:
        print(f"==> Alias {domain}:{cfg.https_port} (same certificate, redirects to the primary)")
        if fetch_cert(cfg, domain):
            print("  ok")
        else:
            ok = fail(f"{domain} did not present a trusted certificate on port "
                      f"{cfg.https_port}. Check `deploy-droplet ps` and the Caddy log.")
    print(f"==> Admin console port {cfg.admin_port} (same certificate)")
    if fetch_cert(cfg, cfg.site_domain, cfg.admin_port):
        print(f"  ok -- {cfg.admin_origin}/")
        return ok
    return fail(
        f"the certificate is fine, but {cfg.admin_origin} did not answer from this "
        f"workstation. Allow inbound TCP {cfg.admin_port} in the DigitalOcean Cloud "
        f"Firewall (your own IP is enough), re-run `deploy-droplet bootstrap` so ufw "
        f"allows it too, and check `deploy-droplet ps` shows caddy publishing it."
    )


def _complete_cert(cfg: DeployConfig) -> dict | None:
    """The primary's served certificate, if trusted AND covering every hostname."""
    cert = fetch_cert(cfg, cfg.site_domain)
    return cert if cert and not missing_names(cfg, cert) else None


def ensure(cfg: DeployConfig, *, wait: int = 180, strict: bool = True) -> bool:
    """Make sure the site serves one trusted certificate covering every hostname,
    waiting for the certs service to obtain it.

    strict=False (used at the end of `deploy`) reports problems without failing,
    since the site itself is already up and issuance may simply need DNS time.
    """
    def fail(msg: str) -> bool:
        if strict:
            raise SystemExit(msg)
        print(f"  warning: {msg}")
        return False

    names = ", ".join(cfg.site_domains)
    print(f"==> TLS certificate for {names} (port {cfg.https_port})")
    cert = fetch_cert(cfg, cfg.site_domain)
    if cert and not missing_names(cfg, cert):
        print("  already serving a trusted certificate for every name")
        print(describe(cert))
        return _check_other_listeners(cfg, fail)
    if cert:
        print(f"  serving a trusted certificate, but it lacks: {', '.join(missing_names(cfg, cert))}"
              f" -- the certs service requests a new one when the list changes")
    else:
        print("  none yet (a self-signed placeholder is served until issuance succeeds)")

    dns_ok = all([check_dns(cfg, d) for d in cfg.site_domains])
    port_ok = check_port80(cfg)
    for service, why in (("caddy", "Caddy serves the challenge and the certificate"),
                         ("certs", "the certs service requests the certificate")):
        if not compose_ops.service_running(cfg, service):
            return fail(f"{service} is not running on the droplet ({why}). Run "
                        f"`deploy-droplet deploy` (or `up`) first.")
    path_ok = check_challenge_path(cfg) if dns_ok and port_ok else False
    if not (dns_ok and port_ok and path_ok):
        return fail("fix the problem(s) above, then run `deploy-droplet cert` again. "
                    "The certs service keeps retrying on its own (backing off), so once "
                    "they are fixed the certificate will arrive.")

    previous = diagnose(_log_lines(cfg))
    if previous:
        print("==> Earlier attempts failed:")
        print(f"  {previous}")
        print("  Retrying anyway, in case that has been fixed since.")

    # Restart so issuance is attempted now, instead of waiting out the certs
    # service's retry backoff from an earlier failed attempt.
    print("==> Restarting the certs service to request the certificate now")
    compose_ops.restart_service(cfg, "certs")

    print(f"==> Waiting up to {wait}s for Let's Encrypt", end="", flush=True)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(5)
        print(".", end="", flush=True)
        cert = _complete_cert(cfg)
        if cert:
            print(f"\n  issued for {names}")
            print(describe(cert))
            return _check_other_listeners(cfg, fail)
    print()
    lines = _log_lines(cfg)
    _show_log(lines)
    advice = diagnose(lines)
    if advice:
        return fail(f"no trusted certificate covering every name after {wait}s.\n\n  {advice}")
    return fail(
        f"no trusted certificate covering every name after {wait}s. Check the log "
        f"lines above. Common causes: DNS not yet propagated to Let's Encrypt's "
        f"resolvers, :80 blocked upstream, or a Let's Encrypt rate limit (5 failed "
        f"validations per hostname per hour). The certs service keeps retrying by "
        f"itself; re-run `deploy-droplet cert` to check again."
    )
