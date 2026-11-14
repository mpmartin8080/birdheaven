"""Print an Argon2id hash for ADMIN_PASSWORD_HASH (and a fresh session key).

    python -m birdadmin.hashpw          (from admin/)

Normally you don't need this: `deploy-droplet admin-password` does the same
and writes the values into deploy/.env.prod for you. The password itself is
never written anywhere -- not to a file, argv, or a log.
"""

from __future__ import annotations

import getpass
import secrets
import sys

from argon2 import PasswordHasher


def main() -> int:
    first = getpass.getpass("Recovery password: ")
    if len(first) < 12:
        print("Too short. Use at least 12 characters.", file=sys.stderr)
        return 1
    if first != getpass.getpass("Again: "):
        print("They do not match.", file=sys.stderr)
        return 1
    print()
    print("ADMIN_PASSWORD_HASH=" + PasswordHasher().hash(first))
    print("ADMIN_SESSION_KEY=" + secrets.token_urlsafe(48))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
