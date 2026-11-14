"""One-time (idempotent) setup of a fresh Debian 13 droplet.

Installs Docker Engine from Docker's own apt repository if it is missing, and
opens the firewall for SSH, :80, the HTTPS port and the admin port. Simpler than financesite's:
there is no Volume to check and no data directories to own, because video lives
in Spaces and Caddy's certificates live in a named Docker volume.
"""

from __future__ import annotations

from .config import DeployConfig
from .ssh import run_script

_SCRIPT = r"""
set -euo pipefail

echo "==> Docker Engine"
if ! command -v docker >/dev/null 2>&1; then
    apt-get update
    apt-get install -y ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
    arch="$(dpkg --print-architecture)"
    echo "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $codename stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
    echo "    already installed: $(docker --version)"
fi
systemctl enable --now docker

echo "==> Firewall (ufw): allow SSH, :80, :{https_port} and the admin console on :{admin_port}"
if ! command -v ufw >/dev/null 2>&1; then
    apt-get update
    apt-get install -y ufw
fi
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow {https_port}/tcp
ufw allow {admin_port}/tcp
ufw --force enable

install -d -m 0755 /opt/birdheaven

echo "==> Done. docker: $(docker --version); compose: $(docker compose version --short)"
"""


def bootstrap(cfg: DeployConfig) -> None:
    run_script(cfg, _SCRIPT.format(https_port=cfg.https_port, admin_port=cfg.admin_port))
