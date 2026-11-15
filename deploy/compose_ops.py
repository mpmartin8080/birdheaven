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
REMOTE_CADDY_RUN = "/opt/birdheaven/caddy-run.sh"


def _base_args(cfg: DeployConfig) -> list[str]:
    args = ["docker", "compose"]
    for f in cfg.compose_files:
        args += ["-f", str(f)]
    return [*args, "-p", PROJECT]


def _env(cfg: DeployConfig, tag: str | None) -> dict[str, str]:
    env = docker_env(cfg)
    env.update(envfile.compose_values(cfg))
    env["BIRD_IMAGE"] = cfg.image_name
    env["BIRD_ADMIN_IMAGE"] = cfg.admin_image_name
    env["BIRD_CERTS_IMAGE"] = cfg.certs_image_name
    env["BIRD_SITE_DOMAIN"] = cfg.site_domain
    env["BIRD_SITE_DOMAINS"] = ",".join(cfg.site_domains)
    env["BIRD_SITE_HTTP_ADDRESSES"] = cfg.site_http_addresses
    env["BIRD_SITE_HTTPS_ADDRESSES"] = cfg.site_https_addresses
    env["BIRD_ACME_EMAIL"] = cfg.acme_email
    env["BIRD_ACME_SERVER"] = cfg.acme_server
    env["BIRD_ADMIN_PORT"] = str(cfg.admin_port)
    env["BIRD_SITE_ORIGIN"] = cfg.site_origin
    env["BIRD_HTTPS_PORT"] = str(cfg.https_port)
    env["BIRD_HTTPS_PORT_SUFFIX"] = cfg.https_port_suffix
    env["BIRD_PORT80_MODE"] = cfg.port80_mode
    # A path on the DROPLET: bind-mount sources are resolved by the remote daemon.
    env["BIRD_CADDYFILE"] = REMOTE_CADDYFILE
    env["BIRD_CADDY_RUN"] = REMOTE_CADDY_RUN
    if tag:
        env["IMAGE_TAG"] = tag
    return env


def up(cfg: DeployConfig, tag: str) -> None:
    require_local_docker()
    missing = build_mod.missing_images(cfg, tag)
    if missing:
        raise SystemExit(
            f"{', '.join(missing)} not on {cfg.host}. Build it first "
            f"(`deploy-droplet deploy`), or pick a tag from `deploy-droplet images`."
        )
    if not envfile.value(cfg, "ADMIN_SESSION_KEY"):
        raise SystemExit("ADMIN_SESSION_KEY is not set, so the admin console would refuse "
                         "to start. Run `deploy-droplet admin-password` first.")
    upload_file(cfg, cfg.repo_root / "deploy" / "caddy" / "Caddyfile", REMOTE_CADDYFILE)
    upload_file(cfg, cfg.repo_root / "deploy" / "caddy" / "run.sh", REMOTE_CADDY_RUN)
    subprocess.run([*_base_args(cfg), "up", "-d", "--remove-orphans"],
                   env=_env(cfg, tag), check=True)
    build_mod.tag_as_latest(cfg, tag)
    # Caddy reads its config only at start; a changed Caddyfile needs a restart.
    subprocess.run([*_base_args(cfg), "restart", "caddy"], env=_env(cfg, tag), check=True)


def down(cfg: DeployConfig) -> None:
    require_local_docker()
    subprocess.run([*_base_args(cfg), "down"], env=_env(cfg, None), check=True)


def logs(cfg: DeployConfig, *, follow: bool, tail: int, services: list[str] | None = None) -> None:
    require_local_docker()
    args = [*_base_args(cfg), "logs", "--tail", str(tail)]
    if follow:
        args.append("-f")
    args.extend(services or [])
    subprocess.run(args, env=_env(cfg, None), check=True)


def ps(cfg: DeployConfig) -> None:
    require_local_docker()
    subprocess.run([*_base_args(cfg), "ps"], env=_env(cfg, None), check=True)


def service_running(cfg: DeployConfig, service: str) -> bool:
    require_local_docker()
    result = subprocess.run(
        [*_base_args(cfg), "ps", "--status", "running", "--quiet", service],
        env=_env(cfg, None), capture_output=True, text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def restart_service(cfg: DeployConfig, service: str) -> None:
    require_local_docker()
    subprocess.run([*_base_args(cfg), "restart", service], env=_env(cfg, None), check=True)


def exec_service(cfg: DeployConfig, service: str, command: list[str]) -> bool:
    """Run a command in a running service's container; True on success."""
    require_local_docker()
    result = subprocess.run([*_base_args(cfg), "exec", "-T", service, *command],
                            env=_env(cfg, None), capture_output=True, text=True)
    return result.returncode == 0


def service_logs(cfg: DeployConfig, service: str, *, tail: int) -> str:
    require_local_docker()
    result = subprocess.run(
        [*_base_args(cfg), "logs", "--no-color", "--tail", str(tail), service],
        env=_env(cfg, None), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return result.stdout + result.stderr
