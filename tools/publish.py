"""Publish clips from the command line -- the same code path as the admin console.

The admin console (admin/, https://<your domain>:7777/) is the normal way to
upload and manage clips. This is the scriptable alternative, e.g. for a batch
of files, and it calls exactly the same functions (admin/birdadmin/media.py),
so a clip published either way is identical.

Run from the repository root, on your workstation:

    python -m tools.publish add CLIP.mp4 --title "Cardinal pair" --species "Northern Cardinal"
    python -m tools.publish list
    python -m tools.publish remove 2026-11-14-cardinal-pair [--keep-files]
    python -m tools.publish add CLIP.mov --title Test --local dev/media     (no Spaces)

Settings come from the environment, else deploy/.env.prod (any value may be
an op:// 1Password reference): SPACES_KEY, SPACES_SECRET, SPACES_BUCKET,
SPACES_REGION, and optionally MEDIA_BASE_URL.

Needs ffmpeg/ffprobe on PATH (winget install Gyan.FFmpeg) and
`pip install -r tools/requirements.txt`.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "admin"))

from birdadmin import media  # noqa: E402
from birdadmin.storage import LocalStorage, SpacesStorage, Storage  # noqa: E402
from deploy import onepassword  # noqa: E402
from deploy.config import ENV_FILE, parse_env_file  # noqa: E402


def _storage(args: argparse.Namespace) -> Storage:
    if args.local:
        return LocalStorage(Path(args.local))
    file_values = parse_env_file(ENV_FILE)
    s = {k: os.environ.get(k) or file_values.get(k) or d for k, d in (
        ("SPACES_KEY", ""), ("SPACES_SECRET", ""), ("SPACES_BUCKET", "birdheaven"),
        ("SPACES_REGION", "nyc3"), ("MEDIA_BASE_URL", ""))}
    s = onepassword.resolve(s)
    if not (s["SPACES_KEY"] and s["SPACES_SECRET"]):
        raise SystemExit(f"Missing SPACES_KEY/SPACES_SECRET: set them in {ENV_FILE} or the "
                         f"environment, or use --local DIR.")
    try:
        return SpacesStorage(key=s["SPACES_KEY"], secret=s["SPACES_SECRET"],
                             bucket=s["SPACES_BUCKET"], region=s["SPACES_REGION"],
                             public_base=s["MEDIA_BASE_URL"])
    except ImportError:
        raise SystemExit("boto3 is not installed: pip install -r tools/requirements.txt") from None


def cmd_add(args: argparse.Namespace) -> None:
    src = Path(args.file)
    if not src.is_file():
        raise SystemExit(f"no such file: {src}")
    storage = _storage(args)
    meta = media.ClipMeta(
        title=args.title,
        description=args.description or "",
        species=args.species or [],
        recorded=media.parse_when(args.recorded) if args.recorded else None,
        featured=args.featured,
        poster_at=args.poster_at,
        slug_hint=args.slug,
    )
    with tempfile.TemporaryDirectory(prefix="birdheaven-") as tmp:
        entry = media.publish_clip(storage, src, meta, Path(tmp),
                                   force_transcode=args.transcode, crf=args.crf,
                                   preset=args.preset, progress=lambda m: print(f"==> {m}"))
    print(f"Published {entry['slug']} to {storage.describe()} "
          f"({entry['width']}x{entry['height']}, {entry['duration']}s, "
          f"{entry['bytes'] / 1e6:.1f} MB).")


def cmd_list(args: argparse.Namespace) -> None:
    doc = media.load_catalog(_storage(args))
    for v in doc["videos"]:
        print(f"{v.get('recorded', '')[:16]:16}  {v.get('slug')}  — {v.get('title')}"
              f"  [{', '.join(v.get('species', []))}]")
    print(f"{len(doc['videos'])} clips")


def cmd_remove(args: argparse.Namespace) -> None:
    try:
        media.remove_clip(_storage(args), args.slug, delete_files=not args.keep_files)
    except media.MediaError as err:
        raise SystemExit(str(err)) from None
    print(f"removed {args.slug}" + (" (files kept)" if args.keep_files else " and its files"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.publish", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--local", metavar="DIR", help="use a local folder instead of Spaces")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="process, upload and catalog a clip")
    a.add_argument("file")
    a.add_argument("--title", required=True)
    a.add_argument("--species", action="append", help="repeat for each species")
    a.add_argument("--description")
    a.add_argument("--recorded", help="ISO 8601; default: file metadata, else mtime")
    a.add_argument("--slug", help="default: from the title")
    a.add_argument("--featured", action="store_true")
    a.add_argument("--transcode", action="store_true",
                   help="always re-encode, even if the source is already H.264")
    a.add_argument("--crf", type=int, default=23)
    a.add_argument("--preset", default="slow",
                   help="x264 preset when transcoding (default slow; your PC is faster than the droplet)")
    a.add_argument("--poster-at", type=float)
    a.set_defaults(func=cmd_add)

    sub.add_parser("list").set_defaults(func=cmd_list)

    r = sub.add_parser("remove")
    r.add_argument("slug")
    r.add_argument("--keep-files", action="store_true", help="only remove it from the catalog")
    r.set_defaults(func=cmd_remove)

    args = p.parse_args(argv)
    try:
        args.func(args)
    except media.MediaError as err:
        raise SystemExit(f"error: {err}") from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
