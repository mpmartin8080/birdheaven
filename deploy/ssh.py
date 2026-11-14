"""Two ways of reaching the droplet, and only two.

`run()` executes one command over a plain SSH session -- for bootstrap steps that
have to happen before there is a Docker daemon to talk to at all (installing Docker
itself, preparing directories on the mounted volume).

`docker_env()` returns the environment that points the *local* `docker` and
`docker compose` binaries at the droplet's daemon instead of a local one, over the
same SSH connection. Everything after bootstrap goes through that -- no rsync, no
second copy of the source living on the droplet. The build context, the compose
file interpretation and the deploy settings all stay on your workstation; only
the Docker Engine API calls cross the SSH tunnel.

Both use the key from BIRD_DEPLOY_SSH_KEY (deploy/config.py, settable in
deploy/.env.prod). `run()` can simply pass `-i`; the docker CLI's transport accepts
no such flag, so docker_env() has to work for it -- read that docstring before
changing how the key is passed.
"""

from __future__ import annotations

import atexit
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from .config import DeployConfig

_SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
]


def _require_key(cfg: DeployConfig) -> str:
    if not cfg.ssh_key.is_file():
        raise SystemExit(
            f"No SSH key at {cfg.ssh_key}. Set BIRD_DEPLOY_SSH_KEY -- in "
            f"deploy/.env.prod, or as an environment variable for a one-off -- if "
            f"it lives somewhere else."
        )
    return str(cfg.ssh_key)


def _base_ssh_args(cfg: DeployConfig) -> list[str]:
    return ["ssh", "-i", _require_key(cfg), *_SSH_OPTS, f"{cfg.ssh_user}@{cfg.host}"]


@lru_cache(maxsize=None)
def _ssh_shim_dir(real_ssh: str, key: str) -> str:
    """A directory containing an `ssh` that is the real one plus `-i <key>`.

    Only ever prepended to the PATH of a docker subprocess (see docker_env). Cached
    so one command that shells out repeatedly builds it once, and deleted at exit.
    It holds a path, not the key material.
    """
    for forbidden in ('"', "%") if os.name == "nt" else ('"',):
        if forbidden in key or forbidden in real_ssh:
            raise SystemExit(
                f"The SSH key path {key!r} contains {forbidden!r}, which cannot be "
                f"quoted safely into the wrapper this needs to pass `-i` to the "
                f"docker CLI's own `ssh` call. Move the key somewhere without it, "
                f"or point at it from ~/.ssh/config for this host instead and "
                f"leave BIRD_DEPLOY_SSH_KEY unset."
            )
    directory = tempfile.mkdtemp(prefix="birdheaven-deploy-ssh-")
    atexit.register(shutil.rmtree, directory, True)
    if os.name == "nt":
        # `%*` is the original argument tail verbatim, quoting included, so the
        # single `-o ConnectTimeout=30` argument docker passes stays one argument
        # rather than being re-split into two by cmd.exe.
        #
        # Written with plain "\n" and no newline= override: text mode translates
        # it to CRLF here, which is what a .bat should have. Spelling "\r\n" out
        # would get translated too, leaving "\r\r\n" on every line.
        (Path(directory) / "ssh.bat").write_text(
            "@echo off\n"
            f'"{real_ssh}" -i "{key}" %*\n'
            "exit /b %ERRORLEVEL%\n",
            encoding="utf-8",
        )
    else:
        shim = Path(directory) / "ssh"
        shim.write_text(
            '#!/bin/sh\nexec "{ssh}" -i "{key}" "$@"\n'.format(ssh=real_ssh, key=key),
            encoding="utf-8",
        )
        shim.chmod(0o700)
    return directory


def run(cfg: DeployConfig, remote_command: str, *, check: bool = True) -> int:
    """Run one command on the droplet over plain SSH, streaming output live."""
    args = [*_base_ssh_args(cfg), remote_command]
    result = subprocess.run(args)
    if check and result.returncode != 0:
        raise SystemExit(f"remote command failed (exit {result.returncode}): {remote_command}")
    return result.returncode


def run_script(cfg: DeployConfig, script: str, *, check: bool = True) -> int:
    """Run a multi-line shell script on the droplet, piped over stdin.

    Piping avoids the quoting nightmare of putting a whole script inside one SSH
    command-line argument.

    `input=` is raw bytes, not a `str` with `text=True`, on purpose: on Windows,
    text-mode stdin runs through universal-newline translation on write, which
    turns every `\\n` in the script into `\\r\\n` before it reaches `ssh`. The
    remote bash then sees a stray `\\r` at the end of every line -- harmless on
    the shebang line, fatal on `set -euo pipefail\\r`, which is not a spelling of
    `pipefail` bash recognises. Encoding to bytes ourselves and skipping `text=`
    means Python performs no newline translation at all, so the `\\n`s already in
    the script (a Python triple-quoted string) reach bash unchanged.
    """
    args = [*_base_ssh_args(cfg), "bash", "-s"]
    result = subprocess.run(args, input=script.encode("utf-8"))
    if check and result.returncode != 0:
        raise SystemExit(f"remote script failed (exit {result.returncode})")
    return result.returncode


def check_reachable(cfg: DeployConfig) -> bool:
    return run(cfg, "true", check=False) == 0


def require_remote_docker(cfg: DeployConfig) -> None:
    """Fail early, and say why, if the droplet has not been bootstrapped.

    Without this, a fresh droplet surfaces as the docker CLI's own
    "docker system dial-stdio ... exit status 127" -- accurate, but it reads
    like a connection problem rather than "run bootstrap first".
    """
    result = subprocess.run([*_base_ssh_args(cfg), "command -v docker"],
                            capture_output=True, text=True)
    if result.returncode == 255:
        raise SystemExit(f"Cannot SSH to {cfg.ssh_user}@{cfg.host}: "
                         f"{result.stderr.strip() or 'connection failed'}")
    if result.returncode != 0:
        raise SystemExit(
            f"Docker is not installed on {cfg.host}. This droplet has not been "
            f"bootstrapped yet -- run `deploy-droplet bootstrap` once, then deploy again."
        )


def upload_file(cfg: DeployConfig, local_path: Path, remote_path: str) -> None:
    """Copy one local file to the droplet, byte for byte.

    Why this exists at all: a compose `volumes:` bind mount is resolved by the
    *daemon*, not the client. Every other path in deploy/compose.prod.yaml
    (/mnt/data_vol/...) already lives on the droplet, but deploy/caddy/Caddyfile
    lives on your workstation -- pointing a bind-mount source straight at it would
    tell the remote daemon to look for a Windows path on its own Linux
    filesystem. So the file has to actually be copied over first.

    Not `scp`: a plain `mkdir -p <dir> && cat > <path>` over the same `ssh`
    connection everything else here uses, so this needs no second binary on
    PATH and, like run_script(), takes raw bytes rather than `text=True` --
    Caddyfiles are plain enough that CRLF corruption would likely go unnoticed
    rather than fail loudly, which is worse.

    The remote command is passed as exactly one trailing argv element, like
    run()'s `remote_command`, not split across several (e.g. `"sh", "-c", ...`).
    Given more than one trailing argument, OpenSSH joins them with spaces and
    sends the result as a single line for the remote shell to parse from
    scratch -- which un-does the `-c` argument boundary and quietly runs `sh -c
    mkdir` with no arguments, `-p` and the actual path reduced to ignored
    positional parameters. One argument means one thing to quote correctly:
    ours, not two shells' worth of re-parsing.
    """
    remote_dir = posixpath.dirname(remote_path)
    command = f"mkdir -p '{remote_dir}' && cat > '{remote_path}'"
    args = [*_base_ssh_args(cfg), command]
    result = subprocess.run(args, input=local_path.read_bytes())
    if result.returncode != 0:
        raise SystemExit(f"failed to upload {local_path} to {cfg.host}:{remote_path}")


def docker_env(cfg: DeployConfig) -> dict[str, str]:
    """Environment that redirects the local docker CLI at the droplet's daemon.

    There is no `-i`-equivalent flag on `-H ssh://...`, and no ssh-opts setting on a
    docker context either. The CLI shells out to exactly

        ssh -l <user> -o ConnectTimeout=30 -T -- <host> "docker system dial-stdio"

    looked up on PATH, with no way to add an argument to it. So the configured key
    is applied the only way that argv allows: an `ssh` of our own goes first on the
    PATH *of this subprocess only*, and runs the real one with `-i <key>` ahead of
    whatever docker passed (see _ssh_shim_dir).

    The point is that BIRD_DEPLOY_SSH_KEY then means one thing for every command
    in this package. Without it, `run()` above would use the configured key while
    every docker command -- build, up, down, logs, ps, images, i.e. nearly all of
    them -- silently used whatever OpenSSH tries by default: the same key only for
    as long as the configured one happens to have a default name like
    `~/.ssh/id_ed25519`, and the wrong one the moment it does not.

    Nothing outside the returned dict is touched: os.environ, the real PATH and
    ~/.ssh/config are all left alone, so pointing at the key from ~/.ssh/config
    instead still works -- leave BIRD_DEPLOY_SSH_KEY unset and the default
    applies, and `-i` is a default OpenSSH would have tried anyway.
    """
    env = dict(os.environ)
    env["DOCKER_HOST"] = cfg.docker_host
    key = _require_key(cfg)
    # Resolved against the real PATH, which this never modifies -- so this always
    # finds the system ssh and never a shim left over from an earlier call.
    real_ssh = shutil.which("ssh")
    if real_ssh is None:
        raise SystemExit(
            "No `ssh` on PATH. The docker CLI needs it for DOCKER_HOST=ssh://... "
            "(Windows 10/11 ship it; otherwise install the OpenSSH client)."
        )
    env["PATH"] = _ssh_shim_dir(real_ssh, key) + os.pathsep + env.get("PATH", "")
    return env


def require_local_docker() -> None:
    from shutil import which
    if which("docker") is None:
        print(
            "No `docker` on PATH. Install Docker (the CLI is enough -- it never "
            "needs a local engine here; every command targets the droplet).",
            file=sys.stderr,
        )
        raise SystemExit(1)
