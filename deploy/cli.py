"""python -m deploy <command>

Run from the repository root (or via deploy-droplet.bat). See deploy/README.md.

    bootstrap            Install Docker and open the firewall on a fresh droplet.
    check                Show where each setting in deploy/.env.prod comes from;
                          resolves 1Password references without printing them.
    admin-password       Set the admin console's recovery password (Argon2 hash)
                          and session key in deploy/.env.prod. Prompts; the
                          password itself is never stored.
    build  [--tag TAG]   Build both images (web, admin) on the droplet's own
                          daemon (default tag: git short SHA). No registry.
    deploy [--tag TAG]   build + up, then check the TLS certificate. The normal
                          release command.
    up     [--tag TAG]   `docker compose up -d` without building: re-applies
                          deploy/.env.prod (e.g. the password switch) to the
                          running images (default tag: latest), or rolls back
                          to an older tag, which then becomes `latest`.
    images               Tags on the droplet, newest first (rollback targets).
    prune  [--keep N]    Delete all but the newest N tags (default 5).
    down                 Stop the stack.
    logs   [-f] [--tail N]
    ps                   Container status.
    cert   [--wait S]    Get the site's Let's Encrypt certificate (Caddy's
                          automatic HTTPS, as in financesite) and verify it on
                          both the site port and the admin port: checks DNS
                          and :80, restarts Caddy to trigger issuance, waits
                          for a trusted cert, shows its expiry.
    status               SSH reachability, containers, TLS cert, /api/health.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request

from . import bootstrap as bootstrap_mod
from . import build as build_mod
from . import compose_ops, envfile, tls
from .config import load
from .ssh import check_reachable, require_remote_docker


def _cfg():
    cfg = load()
    cfg.require_target()
    return cfg


def _docker_cfg():
    """_cfg() for commands that talk to the droplet's Docker daemon."""
    cfg = _cfg()
    require_remote_docker(cfg)
    return cfg


def _tag(args) -> str:
    return args.tag or build_mod.git_tag()


def _cmd_bootstrap(args):
    bootstrap_mod.bootstrap(_cfg())


def _cmd_check(args):
    cfg = load()
    print(f"==> Settings in {cfg.env_file}")
    print(f"  {'BIRD_DEPLOY_HOST':28} {cfg.host or '(unset)'}")
    print(f"  {'BIRD_DEPLOY_SITE_DOMAIN':28} {cfg.site_domain or '(unset)'}")
    print(f"  {'BIRD_DEPLOY_ADMIN_PORT':28} {cfg.admin_port}  ->  {cfg.admin_origin if cfg.site_domain else '(set the site domain)'}")
    print(f"  {'images':28} {', '.join(f'{n}:<tag>' for n in cfg.images.values())} "
          f"(on the droplet, no registry)")
    envfile.check(cfg)


def _cmd_admin_password(args):
    import getpass  # noqa: PLC0415
    import secrets  # noqa: PLC0415
    try:
        from argon2 import PasswordHasher  # noqa: PLC0415
    except ImportError:
        raise SystemExit("argon2-cffi is not installed: py -3 -m pip install -r "
                         "deploy/requirements.txt") from None
    cfg = load()
    current = envfile.value(cfg, "ADMIN_SESSION_KEY")
    print("The recovery password is only used while ADMIN_PASSWORD_LOGIN=true -- to "
          "register the first passkey, or to get back in after losing them all.")
    while True:
        first = getpass.getpass("New recovery password (12+ characters): ")
        if len(first) < 12:
            print("Too short.")
            continue
        if first != getpass.getpass("Again: "):
            print("They did not match.")
            continue
        break
    updates = {"ADMIN_PASSWORD_HASH": PasswordHasher().hash(first)}
    if not current or args.rotate_session_key:
        updates["ADMIN_SESSION_KEY"] = secrets.token_urlsafe(48)
    if args.enable:
        updates["ADMIN_PASSWORD_LOGIN"] = "true"
    envfile.set_values(cfg, updates)
    print(f"\nWrote {', '.join(updates)} to {cfg.env_file}.")
    if args.enable:
        print("Password sign-in is ON. Run `deploy-droplet up` (or `deploy`), sign in at "
              f"{cfg.admin_origin}/ and register a passkey, then set "
              "ADMIN_PASSWORD_LOGIN=false and run `deploy-droplet up` again.")
    else:
        print("To use it, set ADMIN_PASSWORD_LOGIN=true and run `deploy-droplet up`.")


def _cmd_build(args):
    cfg = _docker_cfg()
    build_mod.build(cfg, _tag(args), no_cache=args.no_cache)


def _cmd_deploy(args):
    cfg = _docker_cfg()
    tag = _tag(args)
    build_mod.build(cfg, tag, no_cache=args.no_cache)
    compose_ops.up(cfg, tag)
    print()
    # Non-fatal: the site is already up; a cert still pending is reported, not raised.
    tls.ensure(cfg, wait=120, strict=False)
    print(f"\ndeployed {tag} to {cfg.host}\n  site:  {cfg.site_origin}/\n"
          f"  admin: {cfg.admin_origin}/")


def _cmd_up(args):
    # Default `latest` = whatever is running now, so `up` after editing only
    # deploy/.env.prod re-applies settings without needing a matching build.
    compose_ops.up(_docker_cfg(), args.tag or "latest")


def _cmd_cert(args):
    tls.ensure(_docker_cfg(), wait=args.wait, strict=True)


def _cmd_images(args):
    cfg = _docker_cfg()
    rows = build_mod.list_tags(cfg)
    if not rows:
        print(f"No {cfg.image_name} images on {cfg.host} yet.")
    for tag, image_id, created, size in rows:
        print(f"  {tag:24} {image_id:14} {created:18} {size}")


def _cmd_prune(args):
    build_mod.prune(_docker_cfg(), keep=args.keep)


def _cmd_down(args):
    compose_ops.down(_docker_cfg())


def _cmd_logs(args):
    compose_ops.logs(_docker_cfg(), follow=args.follow, tail=args.tail)


def _cmd_ps(args):
    compose_ops.ps(_docker_cfg())


def _cmd_status(args):
    cfg = _cfg()
    print(f"==> SSH to {cfg.ssh_user}@{cfg.host}")
    if not check_reachable(cfg):
        raise SystemExit("  unreachable")
    print("  ok\n\n==> Containers")
    compose_ops.ps(cfg)
    for label, domain, port in cfg.endpoints:
        print(f"\n==> TLS certificate, {label}: {domain}:{port}")
        cert = tls.fetch_cert(cfg, domain, port)
        print(tls.describe(cert) if cert else
              "  no trusted certificate (or port unreachable) -- run `deploy-droplet cert`")
    for url in (f"{cfg.site_origin}/api/health", f"{cfg.admin_origin}/api/health"):
        print(f"\n==> GET {url}")
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
                print(f"  {resp.status} {resp.read(500).decode('utf-8', 'replace')}")
        except Exception as err:  # noqa: BLE001 -- diagnostic only
            print(f"  failed: {err}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m deploy", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap").set_defaults(func=_cmd_bootstrap)
    sub.add_parser("check").set_defaults(func=_cmd_check)
    ap = sub.add_parser("admin-password")
    ap.add_argument("--enable", action="store_true",
                    help="also set ADMIN_PASSWORD_LOGIN=true (first setup / recovery)")
    ap.add_argument("--rotate-session-key", action="store_true",
                    help="also replace ADMIN_SESSION_KEY (signs every admin session out)")
    ap.set_defaults(func=_cmd_admin_password)
    for name, func in (("build", _cmd_build), ("deploy", _cmd_deploy), ("up", _cmd_up)):
        p = sub.add_parser(name)
        p.add_argument("--tag", default=None,
                       help="default: latest" if name == "up" else "default: current git short SHA")
        if name in ("build", "deploy"):
            p.add_argument("--no-cache", action="store_true", help="force a clean rebuild")
        p.set_defaults(func=func)
    cert_p = sub.add_parser("cert")
    cert_p.add_argument("--wait", type=int, default=180, help="seconds to wait for issuance (default 180)")
    cert_p.set_defaults(func=_cmd_cert)
    sub.add_parser("images").set_defaults(func=_cmd_images)
    prune_p = sub.add_parser("prune")
    prune_p.add_argument("--keep", type=int, default=5, help="newest tags to keep (default 5)")
    prune_p.set_defaults(func=_cmd_prune)
    sub.add_parser("down").set_defaults(func=_cmd_down)
    logs_p = sub.add_parser("logs")
    logs_p.add_argument("--follow", "-f", action="store_true")
    logs_p.add_argument("--tail", type=int, default=200)
    logs_p.set_defaults(func=_cmd_logs)
    sub.add_parser("ps").set_defaults(func=_cmd_ps)
    sub.add_parser("status").set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
