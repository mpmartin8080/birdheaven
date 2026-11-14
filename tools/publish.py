"""Publish recorded clips to the Spaces bucket and the site's catalog.

Run from the repository root, on your workstation:

    python -m tools.publish add CLIP.mp4 --title "Cardinal pair" --species "Northern Cardinal"
    python -m tools.publish list
    python -m tools.publish remove 2026-11-14-cardinal-pair [--delete-files]

`add` does, in order:

  1. Re-encode to H.264/AAC MP4 with `+faststart` (or just remux with --copy),
     capped at 1080p, so every browser can stream and seek it natively.
  2. Grab a poster frame as JPEG (a third of the way in, or --poster-at SECONDS).
  3. Upload both, public-read, with a one-year immutable Cache-Control.
  4. Insert the entry into catalog.json (replacing one with the same slug) and
     upload that with a 60-second Cache-Control.

Nothing here runs on the droplet, and the droplet never holds these keys.

Settings come from the environment, else deploy/.env.prod (the same gitignored
file the deploy tool reads; any value may be an op:// 1Password reference):

    SPACES_KEY, SPACES_SECRET    Spaces access key (API > Spaces Keys)
    SPACES_BUCKET                e.g. birdheaven
    SPACES_REGION                e.g. nyc3

Needs `ffmpeg` and `ffprobe` on PATH (winget install Gyan.FFmpeg) and boto3
(pip install -r tools/requirements.txt).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from deploy import onepassword  # noqa: E402
from deploy.config import ENV_FILE, parse_env_file  # noqa: E402

CATALOG_KEY = "catalog.json"
IMMUTABLE = "public, max-age=31536000, immutable"
CATALOG_CACHE = "public, max-age=60"


# ---------------------------------------------------------------- settings

def _settings() -> dict[str, str]:
    file_values = parse_env_file(ENV_FILE)
    out = {}
    for key, default in (("SPACES_KEY", ""), ("SPACES_SECRET", ""),
                         ("SPACES_BUCKET", "birdheaven"), ("SPACES_REGION", "nyc3")):
        out[key] = os.environ.get(key) or file_values.get(key) or default
    out = onepassword.resolve(out)
    missing = [k for k in ("SPACES_KEY", "SPACES_SECRET") if not out[k]]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)}: set them in {ENV_FILE} "
                         f"or the environment (DigitalOcean > API > Spaces Keys).")
    return out


def _client(s: dict[str, str]):
    try:
        import boto3  # noqa: PLC0415 -- optional dependency, only for this tool
    except ImportError:
        raise SystemExit("boto3 is not installed: pip install -r tools/requirements.txt") from None
    return boto3.session.Session().client(
        "s3",
        region_name=s["SPACES_REGION"],
        endpoint_url=f"https://{s['SPACES_REGION']}.digitaloceanspaces.com",
        aws_access_key_id=s["SPACES_KEY"],
        aws_secret_access_key=s["SPACES_SECRET"],
    )


# ---------------------------------------------------------------- ffmpeg

def _need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise SystemExit(f"`{tool}` is not on PATH. Install FFmpeg (winget install Gyan.FFmpeg).")
    return path


def probe(path: Path) -> dict:
    out = subprocess.run(
        [_need("ffprobe"), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    info = json.loads(out)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    tags = info.get("format", {}).get("tags", {}) or {}
    return {
        "duration": float(info.get("format", {}).get("duration") or 0) or None,
        "width": video.get("width"),
        "height": video.get("height"),
        "vcodec": video.get("codec_name"),
        "creation_time": tags.get("creation_time"),
    }


def encode(src: Path, dst: Path, *, copy: bool, crf: int) -> None:
    if copy:
        codec = ["-c", "copy"]
    else:
        codec = [
            "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
            # Cap at 1080p, keep aspect, never upscale, keep dimensions even.
            "-vf", "scale='min(1920,iw)':'-2'",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        ]
    subprocess.run([_need("ffmpeg"), "-hide_banner", "-loglevel", "warning", "-y",
                    "-i", str(src), *codec, "-movflags", "+faststart", str(dst)], check=True)


def poster(src: Path, dst: Path, at: float) -> None:
    subprocess.run([_need("ffmpeg"), "-hide_banner", "-loglevel", "warning", "-y",
                    "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1",
                    "-vf", "scale='min(1280,iw)':'-2'", "-q:v", "3", str(dst)], check=True)


# ---------------------------------------------------------------- catalog

def load_catalog(s3, bucket: str) -> dict:
    try:
        body = s3.get_object(Bucket=bucket, Key=CATALOG_KEY)["Body"].read()
        doc = json.loads(body)
    except s3.exceptions.NoSuchKey:
        doc = {}
    except Exception as err:  # noqa: BLE001
        if "NoSuchKey" in str(err) or "404" in str(err):
            doc = {}
        else:
            raise
    doc.setdefault("version", 1)
    doc.setdefault("videos", [])
    return doc


def save_catalog(s3, bucket: str, doc: dict) -> None:
    doc["updated"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    doc["videos"].sort(key=lambda v: v.get("recorded", ""), reverse=True)
    s3.put_object(Bucket=bucket, Key=CATALOG_KEY, ACL="public-read",
                  Body=json.dumps(doc, indent=2).encode("utf-8"),
                  ContentType="application/json", CacheControl=CATALOG_CACHE)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "clip"


# ---------------------------------------------------------------- commands

def cmd_add(args: argparse.Namespace) -> None:
    src = Path(args.file)
    if not src.is_file():
        raise SystemExit(f"no such file: {src}")
    info = probe(src)

    if args.recorded:
        recorded = datetime.fromisoformat(args.recorded)
    elif info["creation_time"]:
        recorded = datetime.fromisoformat(info["creation_time"].replace("Z", "+00:00"))
    else:
        recorded = datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc)
    if recorded.tzinfo is None:
        recorded = recorded.astimezone()
    recorded = recorded.replace(microsecond=0)

    slug = f"{recorded:%Y-%m-%d}-{args.slug or slugify(args.title)}"
    prefix = f"{recorded:%Y/%m}"
    video_key = f"videos/{prefix}/{slug}.mp4"
    poster_key = f"posters/{prefix}/{slug}.jpg"

    copy = args.copy
    if copy and info["vcodec"] != "h264":
        print(f"note: --copy ignored, source codec is {info['vcodec']}, not h264")
        copy = False

    with tempfile.TemporaryDirectory(prefix="birdheaven-") as tmp:
        out_mp4 = Path(tmp) / f"{slug}.mp4"
        out_jpg = Path(tmp) / f"{slug}.jpg"
        print(f"==> Encoding {src.name} -> {out_mp4.name}" + (" (remux)" if copy else ""))
        encode(src, out_mp4, copy=copy, crf=args.crf)
        final = probe(out_mp4)
        at = args.poster_at if args.poster_at is not None else (final["duration"] or 3) / 3
        print(f"==> Poster frame at {at:.1f}s")
        poster(out_mp4, out_jpg, at)

        entry = {
            "slug": slug,
            "title": args.title,
            "description": args.description or "",
            "species": args.species or [],
            "recorded": recorded.isoformat(),
            "duration": round(final["duration"] or 0, 1) or None,
            "video": video_key,
            "poster": poster_key,
            "width": final["width"],
            "height": final["height"],
            "bytes": out_mp4.stat().st_size,
            "featured": args.featured,
        }
        print(f"    {entry['width']}x{entry['height']}, {entry['duration']}s, "
              f"{entry['bytes'] / 1e6:.1f} MB")

        if args.dry_run:
            keep = Path(args.dry_run)
            keep.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out_mp4, keep / out_mp4.name)
            shutil.copy2(out_jpg, keep / out_jpg.name)
            print(f"==> Dry run: outputs in {keep}, catalog entry:")
            print(json.dumps(entry, indent=2))
            return

        s = _settings()
        s3 = _client(s)
        bucket = s["SPACES_BUCKET"]
        print(f"==> Uploading to {bucket}/{video_key}")
        s3.upload_file(str(out_mp4), bucket, video_key, ExtraArgs={
            "ACL": "public-read", "ContentType": "video/mp4", "CacheControl": IMMUTABLE})
        s3.upload_file(str(out_jpg), bucket, poster_key, ExtraArgs={
            "ACL": "public-read", "ContentType": "image/jpeg", "CacheControl": IMMUTABLE})

    doc = load_catalog(s3, bucket)
    doc["videos"] = [v for v in doc["videos"] if v.get("slug") != slug] + [entry]
    save_catalog(s3, bucket, doc)
    print(f"==> Published {slug} ({len(doc['videos'])} clips in catalog). "
          f"Live on the site within about a minute.")


def cmd_list(args: argparse.Namespace) -> None:
    s = _settings()
    doc = load_catalog(_client(s), s["SPACES_BUCKET"])
    for v in doc["videos"]:
        print(f"{v.get('recorded', '')[:16]:16}  {v.get('slug')}  — {v.get('title')}"
              f"  [{', '.join(v.get('species', []))}]")
    print(f"{len(doc['videos'])} clips")


def cmd_remove(args: argparse.Namespace) -> None:
    s = _settings()
    s3 = _client(s)
    bucket = s["SPACES_BUCKET"]
    doc = load_catalog(s3, bucket)
    entry = next((v for v in doc["videos"] if v.get("slug") == args.slug), None)
    if not entry:
        raise SystemExit(f"{args.slug} is not in the catalog")
    doc["videos"].remove(entry)
    save_catalog(s3, bucket, doc)
    print(f"removed {args.slug} from the catalog")
    if args.delete_files:
        for key in (entry.get("video"), entry.get("poster")):
            if key:
                s3.delete_object(Bucket=bucket, Key=key)
                print(f"deleted {key}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.publish", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="encode, upload and catalog a clip")
    a.add_argument("file")
    a.add_argument("--title", required=True)
    a.add_argument("--species", action="append", help="repeat for each species")
    a.add_argument("--description")
    a.add_argument("--recorded", help="ISO 8601; default: file metadata, else mtime")
    a.add_argument("--slug", help="default: from the title")
    a.add_argument("--featured", action="store_true", help="prefer as the offline highlight")
    a.add_argument("--copy", action="store_true", help="remux only (source already H.264)")
    a.add_argument("--crf", type=int, default=23, help="x264 quality, lower = bigger (default 23)")
    a.add_argument("--poster-at", type=float, help="seconds into the clip for the poster")
    a.add_argument("--dry-run", metavar="DIR", help="encode into DIR and stop; upload nothing")
    a.set_defaults(func=cmd_add)

    sub.add_parser("list").set_defaults(func=cmd_list)

    r = sub.add_parser("remove")
    r.add_argument("slug")
    r.add_argument("--delete-files", action="store_true", help="also delete the MP4 and poster")
    r.set_defaults(func=cmd_remove)

    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
