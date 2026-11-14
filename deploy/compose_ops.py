"""`docker compose` against the droplet's daemon, from your workstation.

Same approach as financesite/deploy/compose_ops.py: values from deploy/.env.prod
are merged into the *subprocess* environment (never via `--env-file`, which
re-interpolates `$`), and Compose substitutes them into the `${VAR}` references
in deploy/compose.prod.yaml once.
"""

from __future__ import annotations

import subprocess

from . import build as build_mod
from . import envfile
from .config import DeployConfig
from .ssh import docker_env, require_local_docker, upload_file

PROJECT = "birdheaven"
REMOTE_CADDYFILE = "/opt/birdheaven/Caddyfile"


def _base_args(cfg: DeployConfig) -> list[str]:
    args = ["docker", "compose"]
    for f in cfg.compose_files:
        args += ["-f", str(f)]
    return [*args, "-p", PROJECT]


def _env(cfg: DeployConfig, tag: str | None) -> dict[str, str]:
    env = docker_env(cfg)
    env.update(envfile.compose_values(cfg))
    env["BIRD_IMAGE"] = cfg.image_name
    env["BIRD_SITE_DOMAIN"] = cfg.site_domain
    env["BIRD_HTTPS_PORT"] = str(cfg.https_port)
    env["BIRD_HTTPS_PORT_SUFFIX"] = cfg.https_port_suffix
    env["BIRD_PORT80_MODE"] = cfg.port80_mode
    # A path on the DROPLET: bind-mount sources are resolved by the remote daemon.
    env["BIRD_CADDYFILE"] = REMOTE_CADDYFILE
    if tag:
        env["IMAGE_TAG"] = tag
    return env


def up(cfg: DeployConfig, tag: str) -> None:
    require_local_docker()
    if not build_mod.image_exists(cfg, tag):
        raise SystemExit(
            f"{cfg.image_ref(tag)} is not on {cfg.host}. Build it first "
            f"(`deploy-droplet deploy`), or pick a tag from `deploy-droplet images`."
        )
    upload_file(cfg, cfg.repo_root / "deploy" / "caddy" / "Caddyfile", REMOTE_CADDYFILE)
    subprocess.run([*_base_args(cfg), "up", "-d", "--remove-orphans"],
                   env=_env(cfg, tag), check=True)
    build_mod.tag_as_latest(cfg, tag)
    # Caddy reads its config only at start; a changed Caddyfile needs a restart.
    subprocess.run([*_base_args(cfg), "restart", "caddy"], env=_env(cfg, tag), check=True)


def down(cfg: DeployConfig) -> None:
    require_local_docker()
    subprocess.run([*_base_args(cfg), "down"], env=_env(cfg, None), check=True)


def logs(cfg: DeployConfig, *, follow: bool, tail: int) -> None:
    require_local_docker()
    args = [*_base_args(cfg), "logs", "--tail", str(tail)]
    if follow:
        args.append("-f")
    subprocess.run(args, env=_env(cfg, None), check=True)


def ps(cfg: DeployConfig) -> None:
    require_local_docker()
    subprocess.run([*_base_args(cfg), "ps"], env=_env(cfg, None), check=True)
