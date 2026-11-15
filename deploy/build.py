"""Build the images on the droplet's own Docker daemon, and manage what's there.

There is no container registry. The build context (the repo minus
.dockerignore -- a few hundred KB) is streamed from your workstation to the
droplet's daemon over SSH (`DOCKER_HOST=ssh://...`), and the finished images
stay in the droplet's local image store, both tagged with the same git SHA:

    birdheaven-web:<sha>     app/Dockerfile    the public site
    birdheaven-admin:<sha>   admin/Dockerfile  the admin console
    birdheaven-certs:<sha>   deploy/certs/Dockerfile  the SAN certificate (lego)

`docker compose` then runs them from there with `pull_policy: never`. Every
deployed tag stays on the droplet until pruned, so rolling back is just
`deploy-droplet up --tag <older sha>` -- see `images` and `prune` in cli.py.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from .config import DeployConfig
from .ssh import docker_env, require_local_docker

DOCKERFILES = {"web": "app/Dockerfile", "admin": "admin/Dockerfile",
               "certs": "deploy/certs/Dockerfile"}


def git_tag() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=None, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sha = result.stdout.strip()

    dirty = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=False,
    )
    return f"{sha}-dirty" if dirty.stdout.strip() else sha


def build(cfg: DeployConfig, tag: str, *, no_cache: bool = False) -> None:
    require_local_docker()
    for service, dockerfile in DOCKERFILES.items():
        ref = cfg.image_ref(tag, service)
        print(f"==> Building {ref} on {cfg.host}")
        subprocess.run(
            [
                "docker", "build",
                *(["--no-cache"] if no_cache else []),
                "-f", str(cfg.repo_root / dockerfile),
                "-t", ref,
                str(cfg.repo_root),
            ],
            env=docker_env(cfg),
            check=True,
        )


def missing_images(cfg: DeployConfig, tag: str) -> list[str]:
    """Image refs for `tag` that are not on the droplet."""
    out = []
    for service in cfg.images:
        ref = cfg.image_ref(tag, service)
        result = subprocess.run(["docker", "image", "inspect", ref],
                                env=docker_env(cfg), capture_output=True)
        if result.returncode != 0:
            out.append(ref)
    return out


def tag_as_latest(cfg: DeployConfig, tag: str) -> None:
    """Point `:latest` at whatever was just started, so it always means 'running'."""
    if tag == "latest":
        return
    for service in cfg.images:
        subprocess.run(["docker", "tag", cfg.image_ref(tag, service),
                        cfg.image_ref("latest", service)],
                       env=docker_env(cfg), check=True)


def list_tags(cfg: DeployConfig, service: str = "web") -> list[tuple[str, str, str, str]]:
    """(tag, image id, created, size) for every tag of one image, newest first."""
    require_local_docker()
    out = subprocess.run(
        ["docker", "image", "ls", cfg.images[service],
         "--format", "{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}\t{{.Size}}"],
        env=docker_env(cfg), capture_output=True, text=True, check=True,
    ).stdout
    return [tuple(line.split("\t")) for line in out.splitlines() if line.strip()]  # type: ignore[misc]


def prune(cfg: DeployConfig, keep: int) -> None:
    """Remove all but the newest `keep` tags of each image. Never removes `latest`'s."""
    for service in cfg.images:
        rows = list_tags(cfg, service)
        latest_id = next((r[1] for r in rows if r[0] == "latest"), None)
        tagged = [r for r in rows if r[0] not in ("latest", "<none>")]
        doomed = [r for r in tagged[keep:] if r[1] != latest_id]
        if not doomed:
            print(f"{cfg.images[service]}: nothing to prune ({len(tagged)} tags, keeping {keep}).")
        for tag, *_ in doomed:
            print(f"==> Removing {cfg.image_ref(tag, service)}")
            subprocess.run(["docker", "image", "rm", cfg.image_ref(tag, service)],
                           env=docker_env(cfg), check=False)
    # Layers no tag refers to any more, plus the build cache older than a week.
    subprocess.run(["docker", "image", "prune", "-f"], env=docker_env(cfg), check=False)
    subprocess.run(["docker", "builder", "prune", "-f", "--filter", "until=168h"],
                   env=docker_env(cfg), check=False)
