"""Where the bucket's objects live: DigitalOcean Spaces, or a local folder.

Both backends expose the same handful of operations on keys laid out exactly
like the bucket (see ARCHITECTURE.md):

    catalog.json, site-settings.json, videos/YYYY/MM/<slug>.mp4, posters/...

`LocalStorage` exists so the whole admin console -- uploads, editing,
deleting, settings -- runs in local development with no Spaces account. Point
it at dev/media, which the web service already serves as /media.

Shared by the admin container and tools/publish.py.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Protocol

IMMUTABLE = "public, max-age=31536000, immutable"
SHORT = "public, max-age=30"


class Storage(Protocol):
    def get_json(self, key: str) -> Any | None: ...
    def put_json(self, key: str, doc: Any, *, cache_control: str = SHORT) -> None: ...
    def put_file(self, key: str, path: Path, content_type: str,
                 *, cache_control: str = IMMUTABLE) -> None: ...
    def delete(self, key: str) -> None: ...
    def read_url(self, key: str) -> str:
        """Something ffmpeg can read the object from (a URL or a local path)."""
        ...
    def public_url(self, key: str) -> str: ...
    def describe(self) -> str: ...


class LocalStorage:
    def __init__(self, root: Path, public_base: str = "/media") -> None:
        self.root = root.resolve()
        self.public_base = public_base.rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents and p != self.root:
            raise ValueError(f"key escapes storage root: {key}")
        return p

    def get_json(self, key: str) -> Any | None:
        p = self._path(key)
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

    def put_json(self, key: str, doc: Any, *, cache_control: str = SHORT) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        # mkstemp creates 0600. Everything in the "bucket" is public by design,
        # and the web container runs as a different uid and must read it.
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)

    def put_file(self, key: str, path: Path, content_type: str,
                 *, cache_control: str = IMMUTABLE) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, p)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def read_url(self, key: str) -> str:
        return str(self._path(key))

    def public_url(self, key: str) -> str:
        return f"{self.public_base}/{key}"

    def describe(self) -> str:
        return f"local folder {self.root}"


class SpacesStorage:
    def __init__(self, *, key: str, secret: str, bucket: str, region: str,
                 public_base: str = "") -> None:
        import boto3  # noqa: PLC0415 -- only needed for this backend
        from botocore.config import Config as BotoConfig  # noqa: PLC0415

        self.bucket = bucket
        self.region = region
        self.public_base = (public_base or
                            f"https://{bucket}.{region}.cdn.digitaloceanspaces.com").rstrip("/")
        self._s3 = boto3.session.Session().client(
            "s3",
            region_name=region,
            endpoint_url=f"https://{region}.digitaloceanspaces.com",
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            config=BotoConfig(retries={"max_attempts": 5, "mode": "standard"}),
        )

    def get_json(self, key: str) -> Any | None:
        try:
            body = self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self._s3.exceptions.NoSuchKey:
            return None
        except Exception as err:  # noqa: BLE001 -- botocore wraps 404s variously
            if "NoSuchKey" in str(err) or "404" in str(err):
                return None
            raise
        return json.loads(body)

    def put_json(self, key: str, doc: Any, *, cache_control: str = SHORT) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=key, ACL="public-read",
                            Body=json.dumps(doc, indent=2).encode("utf-8"),
                            ContentType="application/json", CacheControl=cache_control)

    def put_file(self, key: str, path: Path, content_type: str,
                 *, cache_control: str = IMMUTABLE) -> None:
        self._s3.upload_file(str(path), self.bucket, key, ExtraArgs={
            "ACL": "public-read", "ContentType": content_type, "CacheControl": cache_control})

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=key)

    def read_url(self, key: str) -> str:
        # Presigned origin URL rather than the CDN, so ffmpeg sees the object
        # as it is now and range requests go straight to Spaces.
        return self._s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=3600)

    def public_url(self, key: str) -> str:
        return f"{self.public_base}/{key}"

    def describe(self) -> str:
        return f"Spaces bucket {self.bucket} ({self.region})"
