"""Storage interface: local (default) or S3-compatible cloud (AWS S3, Cloudflare R2, Backblaze B2).

Cloud storage is REQUIRED for GitHub Actions: runners are temporary and cannot see your PC's folders.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import Config, secret

log = logging.getLogger(__name__)


class StorageError(Exception):
    pass


class Storage:
    name = "base"

    def sync_down(self) -> dict:           # videos + music -> local folders
        return {}

    def pull_state(self, local_db: Path) -> bool:
        return False

    def push_state(self, local_db: Path) -> bool:
        return False

    def upload_reel(self, local_path: Path) -> str | None:
        return None

    def public_url(self, key: str) -> str | None:
        return None


class LocalStorage(Storage):
    name = "local"


class S3Storage(Storage):
    name = "s3"

    def __init__(self, cfg: Config, client=None):
        self.cfg = cfg
        self.bucket = secret("S3_BUCKET")
        if not self.bucket:
            raise StorageError("S3_BUCKET is not set")
        if client is None:
            try:
                import boto3
            except ImportError as e:
                raise StorageError("boto3 not installed: pip install boto3") from e
            kwargs = {"aws_access_key_id": secret("S3_ACCESS_KEY_ID") or None,
                      "aws_secret_access_key": secret("S3_SECRET_ACCESS_KEY") or None,
                      "region_name": secret("S3_REGION") or None}
            if secret("S3_ENDPOINT_URL"):
                kwargs["endpoint_url"] = secret("S3_ENDPOINT_URL")
            client = boto3.client("s3", **kwargs)
        self.s3 = client

    def _pfx(self, key: str) -> str:
        return self.cfg.get(f"storage.s3.{key}", "")

    def _sync_prefix(self, prefix: str, dest: Path, exts: set[str]) -> int:
        dest.mkdir(parents=True, exist_ok=True)
        n = 0
        token = None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**kw)
            for obj in resp.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/") or Path(key).suffix.lower() not in exts:
                    continue
                target = dest / Path(key).name
                if target.exists() and target.stat().st_size == obj["Size"]:
                    continue
                log.info("Downloading %s", key)
                self.s3.download_file(self.bucket, key, str(target))
                n += 1
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return n

    def sync_down(self) -> dict:
        from .scanner import VIDEO_EXTS
        v = self._sync_prefix(self._pfx("video_prefix"), self.cfg.path("paths.video_dir"), VIDEO_EXTS)
        m = self._sync_prefix(self._pfx("music_prefix"), self.cfg.path("paths.music_dir"),
                              {e.lower() for e in self.cfg.get("music.extensions", [])})
        return {"videos_downloaded": v, "music_downloaded": m}

    def pull_state(self, local_db: Path) -> bool:
        try:
            local_db.parent.mkdir(parents=True, exist_ok=True)
            self.s3.download_file(self.bucket, self._pfx("state_key"), str(local_db))
            return True
        except Exception as e:  # first run: no state yet
            log.info("No remote state found (%s) - starting fresh", type(e).__name__)
            return False

    def push_state(self, local_db: Path) -> bool:
        if not local_db.exists():
            return False
        self.s3.upload_file(str(local_db), self.bucket, self._pfx("state_key"))
        return True

    def upload_reel(self, local_path: Path) -> str:
        key = f"{self._pfx('reel_prefix')}{local_path.name}"
        self.s3.upload_file(str(local_path), self.bucket, key, ExtraArgs={"ContentType": "video/mp4"})
        return key

    def public_url(self, key: str) -> str:
        return self.s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=int(self.cfg.get("storage.s3.presign_seconds", 86400)))


def get_storage(cfg: Config) -> Storage:
    if cfg.get("storage.provider", "local") == "s3":
        return S3Storage(cfg)
    return LocalStorage()
