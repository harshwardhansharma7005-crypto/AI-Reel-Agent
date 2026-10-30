"""Instagram Graph API publishing (Reels) with gating, retries, status polling, duplicate prevention.

Official docs (verify current details before going live):
  https://developers.facebook.com/docs/instagram-platform/content-publishing
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import Config, secret
from .db import Database

log = logging.getLogger(__name__)
TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 613}


class PublishError(Exception):
    pass


class AmbiguousPublishError(PublishError):
    """media_publish may or may not have succeeded. Do NOT auto-retry (would risk a duplicate)."""


@dataclass
class PublishResult:
    status: str          # dry_run | skipped | published | failed | blocked | unknown
    detail: str = ""
    media_id: str | None = None
    permalink: str | None = None


def publishing_blockers(cfg: Config, dry_run: bool) -> list[str]:
    """Reasons that stop a real publish. Empty list = allowed."""
    b = []
    if dry_run or cfg.get("publishing.dry_run", True):
        b.append("dry-run mode is on")
    if not cfg.get("publishing.enabled", False):
        b.append("publishing.enabled is false in settings.yaml")
    if not secret("IG_USER_ID"):
        b.append("IG_USER_ID is not set")
    if not secret("IG_ACCESS_TOKEN"):
        b.append("IG_ACCESS_TOKEN is not set")
    if not cfg.get("music.i_have_rights_to_publish_all_tracks", False):
        b.append("music.i_have_rights_to_publish_all_tracks is false (confirm you may publish your tracks)")
    return b


class InstagramClient:
    def __init__(self, cfg: Config, session: requests.Session | None = None, sleep=time.sleep):
        self.cfg = cfg
        self.http = session or requests.Session()
        self.sleep = sleep
        host = cfg.get("publishing.api_host", "graph.facebook.com")
        self.base = f"https://{host}/{cfg.get('publishing.api_version', 'v21.0')}"
        self.user_id = secret("IG_USER_ID")
        self.token = secret("IG_ACCESS_TOKEN")
        self.max_retries = int(cfg.get("publishing.max_retries", 3))

    def _call(self, method: str, url: str, retry: bool = True, **kw) -> dict:
        params = dict(kw.pop("params", {}) or {})
        data = kw.pop("data", None)
        headers = kw.pop("headers", {})
        params["access_token"] = self.token
        attempts = self.max_retries if retry else 1
        last = ""
        for i in range(attempts):
            try:
                r = self.http.request(method, url, params=params if method == "GET" else None,
                                      data=({**(data or {}), "access_token": self.token} if method == "POST" and not headers
                                            else data),
                                      headers=headers or None, timeout=120, **kw)
            except requests.RequestException as e:
                last = f"network error: {type(e).__name__}"
                if not retry:
                    raise AmbiguousPublishError(last) from e
            else:
                try:
                    body = r.json()
                except ValueError:
                    body = {}
                if r.status_code < 300 and "error" not in body:
                    return body
                err = body.get("error", {}) if isinstance(body, dict) else {}
                last = f"HTTP {r.status_code} code={err.get('code')} {err.get('message', r.text[:200])}"
                transient = r.status_code >= 500 or err.get("code") in TRANSIENT_CODES
                if not transient:
                    raise PublishError(last)
                if not retry:
                    raise AmbiguousPublishError(last)
            if i < attempts - 1:
                self.sleep(min(2 ** (i + 1) * 5, 60))
        raise PublishError(f"Gave up after {attempts} attempts: {last}")

    def remaining_quota(self) -> int | None:
        try:
            d = self._call("GET", f"{self.base}/{self.user_id}/content_publishing_limit",
                           params={"fields": "quota_usage,config"})
            item = d["data"][0]
            return int(item["config"]["quota_total"]) - int(item["quota_usage"])
        except Exception as e:
            log.warning("Could not read publishing quota: %s", e)
            return None

    def create_container(self, caption: str, video_url: str | None = None) -> dict:
        params = {"media_type": "REELS", "caption": caption,
                  "share_to_feed": "true" if self.cfg.get("publishing.share_to_feed", True) else "false"}
        if video_url:
            params["video_url"] = video_url
        else:
            params["upload_type"] = "resumable"
        d = self._call("POST", f"{self.base}/{self.user_id}/media", data=params)
        if not d.get("id"):
            raise PublishError("Container creation returned no id")
        return {"id": d["id"], "uri": d.get("uri")}

    def upload_resumable(self, container_id: str, path: Path, upload_uri: str | None = None) -> None:
        size = path.stat().st_size
        host = "rupload.facebook.com"
        url = upload_uri or f"https://{host}/ig-api-upload/{self.cfg.get('publishing.api_version', 'v21.0')}/{container_id}"

        headers = {
            "Authorization": f"OAuth {self.token}",
            "offset": "0",
            "file_size": str(size),
        }

        with open(path, "rb") as f:
            r = self.http.post(url, headers=headers, data=f, timeout=600)

        if r.status_code >= 300:
            raise PublishError(
                f"Resumable upload failed: HTTP {r.status_code} {r.text[:200]}"
            )

        try:
            result = r.json()
        except ValueError:
            raise PublishError(
                f"Resumable upload returned invalid JSON: HTTP {r.status_code}"
            ) from None

        if result.get("success") is not True:
            raise PublishError(
                f"Resumable upload was not confirmed: {str(result)[:200]}"
            )

    def wait_until_ready(self, container_id: str) -> None:
        deadline = time.monotonic() + int(
            self.cfg.get("publishing.poll_timeout_seconds", 600)
        )
        interval = int(self.cfg.get("publishing.poll_interval_seconds", 10))

        while True:
            d = self._call(
                "GET",
                f"{self.base}/{container_id}",
                params={"fields": "id,status,status_code,video_status"},
            )
            code = d.get("status_code")
            status = d.get("status", "")
            video_status = d.get("video_status", "")
            log.info(
                "Container %s status_code=%s status=%s video_status=%s",
                container_id, code, status, video_status,
            )

            if code == "FINISHED":
                return

            if code in ("ERROR", "EXPIRED"):
                raise PublishError(
                    f"Instagram processing failed: status_code={code}, "
                    f"status={status}, video_status={video_status}"
                )

            if not code:
                raise PublishError(
                    f"Instagram returned no status_code for container {container_id}: "
                    f"{str(d)[:300]}"
                )

            if time.monotonic() > deadline:
                raise PublishError(
                    f"Timed out waiting for Instagram processing; "
                    f"last status_code={code}, status={status}, "
                    f"video_status={video_status}"
                )

            self.sleep(interval)

    def publish_container(self, container_id: str) -> str:
        d = self._call("POST", f"{self.base}/{self.user_id}/media_publish", retry=False,
                       data={"creation_id": container_id})
        if "id" not in d:
            raise AmbiguousPublishError("media_publish returned no id")
        return d["id"]

    def permalink(self, media_id: str) -> str | None:
        try:
            return self._call("GET", f"{self.base}/{media_id}", params={"fields": "permalink"}).get("permalink")
        except PublishError:
            return None


def publish_reel(cfg: Config, db: Database, post_id: int, slot_key: str, video_hash: str, reel_path: Path,
                 caption: str, dry_run: bool, storage=None, client: InstagramClient | None = None) -> PublishResult:
    """Gate -> duplicate checks -> upload -> poll -> publish -> verify. Never raises for expected failures."""
    blockers = publishing_blockers(cfg, dry_run)
    if blockers:
        status = "dry_run" if (dry_run or cfg.get("publishing.dry_run", True)) else "blocked"
        db.update_post(post_id, status=status, error="; ".join(blockers) if status == "blocked" else None)
        log.info("Not publishing: %s", "; ".join(blockers))
        return PublishResult(status, "; ".join(blockers))
    # duplicate prevention
    prior = db.get_post_for_slot(slot_key)
    if prior and prior["id"] != post_id:
        db.update_post(post_id, status="skipped", error=f"slot {slot_key} already {prior['status']}")
        return PublishResult("skipped", f"slot already {prior['status']}")
    days = int(cfg.get("dedupe.video_cooldown_days", 30))
    if db.recently_published_video(video_hash, days):
        db.update_post(post_id, status="skipped", error="video published recently")
        return PublishResult("skipped", f"video published within {days} days")
    client = client or InstagramClient(cfg)
    quota = client.remaining_quota()
    if quota is not None and quota < int(cfg.get("publishing.min_remaining_quota", 1)):
        db.update_post(post_id, status="failed", error="Instagram publishing quota exhausted")
        return PublishResult("failed", "quota exhausted")
    db.update_post(post_id, status="publishing")
    try:
        if cfg.get("publishing.upload_mode", "url") == "url":
            if storage is None:
                raise PublishError("upload_mode=url needs cloud storage (storage.provider: s3) to host the video")
            key = storage.upload_reel(reel_path)
            url = storage.public_url(key) if key else None
            if not url:
                raise PublishError("Storage did not return a public URL")
            container_data = client.create_container(caption, video_url=url)
        else:
            container_data = client.create_container(caption)
            client.upload_resumable(container_data["id"], reel_path, container_data.get("uri"))

        container_id = container_data["id"]
        db.update_post(post_id, container_id=container_id)
        client.wait_until_ready(container_id)
        media_id = client.publish_container(container_id)
    except AmbiguousPublishError as e:
        db.update_post(post_id, status="unknown", error=str(e))
        log.error("Publish outcome unknown - check Instagram manually before retrying: %s", e)
        return PublishResult("unknown", str(e))
    except PublishError as e:
        db.update_post(post_id, status="failed", error=str(e))
        log.error("Publish failed: %s", e)
        return PublishResult("failed", str(e))
    link = client.permalink(media_id)
    db.update_post(post_id, status="published", media_id=media_id, permalink=link)
    log.info("Published media %s %s", media_id, link or "")
    return PublishResult("published", "ok", media_id, link)
