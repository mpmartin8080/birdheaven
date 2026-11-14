"""Where the droplet is, and the handful of things that are true about it.

Adapted from financesite/deploy/config.py. One difference: every setting here
can be written in deploy/.env.prod, not just the SSH key, because this site has
no "one real target" to default to yet -- the droplet IP and domain have to be
filled in once. Precedence for each setting: OS environment, then
deploy/.env.prod, then the default below (see `_setting()`).
"""

from __future__ import annotations

import os
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
    compose_files: tuple[Path, ...]
    env_file: Path
    site_domain: str
    https_port: int
    port80_mode: str
    repo_root: Path = REPO_ROOT

    @property
    def https_port_suffix(self) -> str:
        """The HTTPS port as written inside a URL: empty for 443."""
        return "" if self.https_port == 443 else f":{self.https_port}"

    @property
    def site_origin(self) -> str:
        return f"https://{self.site_domain}{self.https_port_suffix}"

    def image_ref(self, tag: str) -> str:
        # A plain local name: the image only ever exists in the droplet's own
        # image store. There is no registry (see deploy/build.py).
        return f"{self.image_name}:{tag}"

    @property
    def docker_host(self) -> str:
        return f"ssh://{self.ssh_user}@{self.host}"

    def require_target(self) -> None:
        missing = [name for name, value in (("BIRD_DEPLOY_HOST", self.host),
                                            ("BIRD_DEPLOY_SITE_DOMAIN", self.site_domain))
                   if not value]
        if missing:
            raise SystemExit(
                f"Set {' and '.join(missing)} in {self.env_file} (copy "
                f"deploy/.env.example to start). There is no default droplet for "
                f"this site -- see ARCHITECTURE.md, 'Hosting note'."
            )


def load() -> DeployConfig:
    fv = parse_env_file(ENV_FILE)
    ssh_key = Path(_setting("BIRD_DEPLOY_SSH_KEY",
                            str(Path.home() / ".ssh" / "id_ed25519"), fv)).expanduser()
    return DeployConfig(
        host=_setting("BIRD_DEPLOY_HOST", "", fv),
        ssh_user=_setting("BIRD_DEPLOY_USER", "root", fv),
        ssh_key=ssh_key,
        image_name=_setting("BIRD_DEPLOY_IMAGE", "birdheaven-web", fv),
        compose_files=(
            REPO_ROOT / "app" / "compose.yaml",
            REPO_ROOT / "deploy" / "compose.prod.yaml",
        ),
        env_file=ENV_FILE,
        site_domain=_setting("BIRD_DEPLOY_SITE_DOMAIN", "", fv),
        https_port=int(_setting("BIRD_DEPLOY_HTTPS_PORT", "443", fv)),
        # "redirect" (permanent) or "passthrough" (bring-up only, before DNS and
        # the certificate work). See deploy/caddy/Caddyfile.
        port80_mode=_setting("BIRD_DEPLOY_PORT80_MODE", "redirect", fv),
    )
