"""python -m deploy <command>

Run from the repository root (or via deploy-droplet.bat). See deploy/README.md.

    bootstrap            Install Docker and open the firewall on a fresh droplet.
    check                Show where each setting in deploy/.env.prod comes from;
                          resolves 1Password references without printing them.
    build  [--tag TAG]   Build the image on the droplet's own daemon (default
                          tag: git short SHA). No registry: it stays there.
    deploy [--tag TAG]   build + up. The normal release command.
    up     [--tag TAG]   `docker compose up -d` with a tag already built on the
                          droplet (e.g. to roll back). Retags it as `latest`.
    images               Tags on the droplet, newest first (rollback targets).
    prune  [--keep N]    Delete all but the newest N tags (default 5).
    down                 Stop the stack.
    logs   [-f] [--tail N]
    ps                   Container status.
    status               SSH reachability, containers, and /api/health over HTTPS.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request

from . import bootstrap as bootstrap_mod
from . import build as build_mod
from . import compose_ops, envfile
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
    print(f"  {'image':28} {cfg.image_ref('<tag>')} (on the droplet, no registry)")
    envfile.check(cfg)


def _cmd_build(args):
    cfg = _docker_cfg()
    build_mod.build(cfg, _tag(args), no_cache=args.no_cache)


def _cmd_deploy(args):
    cfg = _docker_cfg()
    tag = _tag(args)
    build_mod.build(cfg, tag, no_cache=args.no_cache)
    compose_ops.up(cfg, tag)
    print(f"\ndeployed {cfg.image_ref(tag)} to {cfg.host}\n  {cfg.site_origin}/")


def _cmd_up(args):
    compose_ops.up(_docker_cfg(), _tag(args))


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
    url = f"{cfg.site_origin}/api/health"
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
    for name, func in (("build", _cmd_build), ("deploy", _cmd_deploy), ("up", _cmd_up)):
        p = sub.add_parser(name)
        p.add_argument("--tag", default=None, help="default: current git short SHA")
        if name in ("build", "deploy"):
            p.add_argument("--no-cache", action="store_true", help="force a clean rebuild")
        p.set_defaults(func=func)
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
