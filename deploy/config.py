"""Where the droplet is, and the handful of things that are true about it.

Adapted from financesite/deploy/config.py. One difference: every setting here
can be written in deploy/.env.prod, not just the SSH key, because this site has
no "one real target" to default to yet -- the droplet IP and domain have to be
filled in once. Precedence for each setting: OS environment, then
deploy/.env.prod, then the default below (see `_setting()`).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / "deploy" / ".env.prod"


def parse_env_file(path: Path) -> dict[str, str]:
    """`KEY=value` lines from an env file; a missing file is an empty dict."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        for quote in ('"', "'"):
            if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
                value = value[1:-1]
                break
        values[key.strip()] = value
    return values


def _setting(name: str, default: str, file_values: dict[str, str]) -> str:
    """OS environment, else deploy/.env.prod, else the default. Empty = unset."""
    return os.environ.get(name) or file_values.get(name) or default


@dataclass(frozen=True)
class DeployConfig:
    host: str
    ssh_user: str
    ssh_key: Path
    image_name: str
    admin_image_name: str
    certs_image_name: str
    compose_files: tuple[Path, ...]
    env_file: Path
    # Every public hostname, primary first (BIRD_DEPLOY_SITE_DOMAIN, comma-
    # separated). All of them are SANs on the one certificate; the others
    # redirect to the primary.
    site_domains: tuple[str, ...]
    admin_port: int
    https_port: int
    port80_mode: str
    acme_email: str
    acme_server: str
    repo_root: Path = REPO_ROOT

    @property
    def site_domain(self) -> str:
        """The primary hostname: the public site and the admin console live here."""
        return self.site_domains[0] if self.site_domains else ""

    @property
    def alias_domains(self) -> tuple[str, ...]:
        """Extra hostnames: on the certificate, redirecting to the primary."""
        return self.site_domains[1:]

    @property
    def site_http_addresses(self) -> str:
        """Caddy site addresses for :80, every hostname."""
        return ", ".join(f"http://{d}" for d in self.site_domains)

    @property
    def site_https_addresses(self) -> str:
        """Caddy site addresses for the HTTPS port, every hostname."""
        return ", ".join(f"{d}:{self.https_port}" for d in self.site_domains)

    @property
    def https_port_suffix(self) -> str:
        """The HTTPS port as written inside a URL: empty for 443."""
        return "" if self.https_port == 443 else f":{self.https_port}"

    @property
    def site_origin(self) -> str:
        return f"https://{self.site_domain}{self.https_port_suffix}"

    @property
    def admin_origin(self) -> str:
        """The admin console: same hostname (and certificate), its own port."""
        return f"https://{self.site_domain}:{self.admin_port}"

    @property
    def endpoints(self) -> tuple[tuple[str, str, int], ...]:
        """(label, hostname, port) for every HTTPS listener Caddy serves.

        All use the one SAN certificate -- a certificate is for hostnames,
        never ports -- so there is one issuance and several places to check.
        """
        return (("site", self.site_domain, self.https_port),
                *(("alias", d, self.https_port) for d in self.alias_domains),
                ("admin", self.site_domain, self.admin_port))

    @property
    def images(self) -> dict[str, str]:
        """service -> image name, for every image built from this repo."""
        return {"web": self.image_name, "admin": self.admin_image_name,
                "certs": self.certs_image_name}

    def image_ref(self, tag: str, service: str = "web") -> str:
        # A plain local name: the image only ever exists in the droplet's own
        # image store. There is no registry (see deploy/build.py).
        return f"{self.images[service]}:{tag}"

    @property
    def docker_host(self) -> str:
        return f"ssh://{self.ssh_user}@{self.host}"

    def require_target(self) -> None:
        missing = [name for name, value in (("BIRD_DEPLOY_HOST", self.host),
                                            ("BIRD_DEPLOY_SITE_DOMAIN", self.site_domain),
                                            ("BIRD_DEPLOY_ACME_EMAIL", self.acme_email))
                   if not value]
        if self.admin_port in (80, self.https_port):
            raise SystemExit(f"BIRD_DEPLOY_ADMIN_PORT ({self.admin_port}) must differ from "
                             f"80 and the site's HTTPS port ({self.https_port}).")
        if missing:
            raise SystemExit(
                f"Set {' and '.join(missing)} in {self.env_file} (copy "
                f"deploy/.env.example to start). There is no default droplet for "
                f"this site -- see ARCHITECTURE.md, 'Hosting note'."
            )


_HOSTNAME = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]*[a-z0-9]$")


def parse_domains(raw: str) -> tuple[str, ...]:
    """'a.example, www.a.example' -> ('a.example', 'www.a.example').

    Lower-cased, de-duplicated, order kept (the first is the primary). Exits
    with a clear message on anything that isn't a plain DNS hostname: no
    scheme, port, path or wildcard (HTTP-01 cannot validate wildcards).
    """
    out: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower().rstrip(".")
        if not name:
            continue
        if not _HOSTNAME.match(name):
            raise SystemExit(f"BIRD_DEPLOY_SITE_DOMAIN: {part.strip()!r} is not a plain hostname "
                             f"(e.g. birdheaven.example.net -- no https://, port, path or *).")
        if name not in out:
            out.append(name)
    if len(out) > 100:
        raise SystemExit("BIRD_DEPLOY_SITE_DOMAIN: Let's Encrypt allows at most 100 names "
                         "on one certificate.")
    return tuple(out)


def load() -> DeployConfig:
    fv = parse_env_file(ENV_FILE)
    ssh_key = Path(_setting("BIRD_DEPLOY_SSH_KEY",
                            str(Path.home() / ".ssh" / "id_ed25519"), fv)).expanduser()
    site_domains = parse_domains(_setting("BIRD_DEPLOY_SITE_DOMAIN", "", fv))
    return DeployConfig(
        host=_setting("BIRD_DEPLOY_HOST", "", fv),
        ssh_user=_setting("BIRD_DEPLOY_USER", "root", fv),
        ssh_key=ssh_key,
        image_name=_setting("BIRD_DEPLOY_IMAGE", "birdheaven-web", fv),
        admin_image_name=_setting("BIRD_DEPLOY_ADMIN_IMAGE", "birdheaven-admin", fv),
        certs_image_name=_setting("BIRD_DEPLOY_CERTS_IMAGE", "birdheaven-certs", fv),
        compose_files=(
            REPO_ROOT / "app" / "compose.yaml",
            REPO_ROOT / "deploy" / "compose.prod.yaml",
        ),
        env_file=ENV_FILE,
        site_domains=site_domains,
        # The admin console's port on the site's own hostname. A different port
        # is a different *origin*, which is what keeps the console's pages and
        # API out of reach of anything served on the public port (see
        # ARCHITECTURE.md, "Admin console"). Open it in the Cloud Firewall --
        # ideally to your own IP only; Let's Encrypt never needs it.
        admin_port=int(_setting("BIRD_DEPLOY_ADMIN_PORT", "7777", fv)),
        https_port=int(_setting("BIRD_DEPLOY_HTTPS_PORT", "443", fv)),
        # "redirect" (permanent) or "passthrough" (bring-up only, before DNS and
        # the certificate work). See deploy/caddy/Caddyfile.
        port80_mode=_setting("BIRD_DEPLOY_PORT80_MODE", "redirect", fv),
        # Let's Encrypt account contact (required: lego won't register an
        # account without one), and CA directory (blank = Let's Encrypt
        # production; the staging URL is handy for testing).
        acme_email=_setting("BIRD_DEPLOY_ACME_EMAIL", "", fv),
        acme_server=_setting("BIRD_DEPLOY_ACME_SERVER", "", fv),
    )
