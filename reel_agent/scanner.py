"""Video library scanning with exact and near-duplicate detection."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .db import Database
from .frames import is_near_duplicate, video_phash
from .media import MediaError, probe

log = logging.getLogger(__name__)
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def file_hash(path: Path, chunk: int = 1 << 20) -> str:
    """Fast content hash: size + first/middle/last 1 MB."""
    size = path.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(chunk))
        if size > 3 * chunk:
            f.seek(size // 2)
            h.update(f.read(chunk))
            f.seek(max(0, size - chunk))
            h.update(f.read(chunk))
    return h.hexdigest()


@dataclass
class ScanResult:
    new: list[str] = field(default_factory=list)
    known: list[str] = field(default_factory=list)
    duplicates: list[tuple[str, str]] = field(default_factory=list)   # (path, duplicate_of_path)
    skipped: list[tuple[str, str]] = field(default_factory=list)      # (path, reason)

    @property
    def total_usable(self) -> int:
        return len(self.new) + len(self.known)


def list_video_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def scan_videos(cfg: Config, db: Database, folder: Path | None = None) -> ScanResult:
    folder = folder or cfg.path("paths.video_dir")
    min_dur = float(cfg.get("analysis.min_source_duration", 6))
    max_dist = int(cfg.get("dedupe.near_duplicate_distance", 12))
    max_color = float(cfg.get("dedupe.near_duplicate_color_diff", 14))
    res = ScanResult()
    seen_this_scan: dict[str, str] = {}   # hash -> path
    for p in list_video_files(folder):
        try:
            h = file_hash(p)
        except OSError as e:
            res.skipped.append((str(p), f"unreadable: {e}"))
            continue
        if h in seen_this_scan:  # exact copy under another name
            res.duplicates.append((str(p), seen_this_scan[h]))
            continue
        existing = db.get_video(h)
        if existing:
            seen_this_scan[h] = str(p)
            db.upsert_video(hash=h, path=str(p), size=existing["size"], duration=existing["duration"],
                            width=existing["width"], height=existing["height"])
            if existing["status"] == "duplicate":
                res.duplicates.append((str(p), existing["duplicate_of"] or ""))
            else:
                res.known.append(str(p))
            continue
        try:
            info = probe(p)
        except MediaError as e:
            res.skipped.append((str(p), str(e)))
            continue
        if not info["has_video"] or info["duration"] < min_dur:
            res.skipped.append((str(p), f"too short or no video stream ({info['duration']:.1f}s)"))
            continue
        sig = video_phash(p)
        dup_of = None
        for row in db.all_videos():
            if row["status"] == "duplicate" or not row["phash"]:
                continue
            if is_near_duplicate(sig, row["phash"], max_dist, max_color):
                dup_of = row["hash"]
                dup_path = row["path"]
                break
        status = "duplicate" if dup_of else "new"
        db.upsert_video(hash=h, path=str(p), size=info["size"], duration=info["duration"], width=info["width"],
                        height=info["height"], phash=sig, status=status, duplicate_of=dup_of)
        seen_this_scan[h] = str(p)
        if dup_of:
            res.duplicates.append((str(p), dup_path))
            log.info("Near-duplicate: %s ~ %s", p.name, Path(dup_path).name)
        else:
            res.new.append(str(p))
    return res
