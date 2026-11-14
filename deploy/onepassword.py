"""Secrets kept in 1Password instead of in deploy/.env.prod.

Any secret in deploy/.env.prod may hold a 1Password secret reference in place of
its value:

    SPACES_KEY=op://Personal/birdheaven-spaces/username
    SPACES_SECRET=op://Personal/birdheaven-spaces/credential

`resolve()` swaps each reference for what `op read` returns, at the moment a
command needs it. The value then takes exactly the route a plain one always has --
a Python dict, merged into the `docker compose` subprocess environment, substituted
once into the container spec (see compose_ops.py) -- so it is never written to a
file on this workstation, never printed, and never put on a command line where
another process could read it. Only the reference is.

Needs the 1Password CLI (`op`) on PATH and signed in. With the desktop app's CLI
integration turned on, the first read of a run asks for approval in the app (or
Windows Hello) and the rest of the run reuses it. Each reference is read once per
run however many times the environment is built.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache

PREFIX = "op://"


def is_reference(value: str | None) -> bool:
    return bool(value) and value.strip().startswith(PREFIX)


def resolve(values: dict[str, str]) -> dict[str, str]:
    """`values` with every 1Password reference replaced by the secret it names."""
    return {key: _read(value.strip()) if is_reference(value) else value
            for key, value in values.items()}


@lru_cache(maxsize=None)
def _read(reference: str) -> str:
    op = shutil.which("op")
    if not op:
        raise SystemExit(
            f"deploy/.env.prod refers to 1Password ({reference}), but the 1Password "
            f"CLI `op` is not on PATH. Install it (winget install AgileBits.1Password.CLI) "
            f"and turn on the desktop app's CLI integration, or put the value in the "
            f"file instead."
        )
    # The reference is an argument, which is fine -- it names a secret, it is not
    # one. The secret only ever comes back on stdout, captured here.
    result = subprocess.run([op, "read", "--no-newline", reference],
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise SystemExit(
            f"Could not read {reference} from 1Password: "
            f"{detail[-1] if detail else f'op exited {result.returncode}'}"
        )
    value = result.stdout
    if not value:
        raise SystemExit(f"{reference} is empty in 1Password.")
    return value
