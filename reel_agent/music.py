"""Music library scanning and non-repeating selection."""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import Database
from .media import MediaError, probe

log = logging.getLogger(__name__)


@dataclass
class Track:
    path: str
    duration: float

    @property
    def name(self) -> str:
        return Path(self.path).name


def scan_music(cfg: Config, folder: Path | None = None) -> list[Track]:
    folder = folder or cfg.path("paths.music_dir")
    exts = {e.lower() for e in cfg.get("music.extensions", [".mp3", ".wav"])}
    min_len = float(cfg.get("music.min_track_seconds", 10))
    tracks = []
    if not folder.exists():
        return tracks
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in exts:
            try:
                info = probe(p)
            except MediaError as e:
                log.warning("Skipping unreadable track %s: %s", p.name, e)
                continue
            if info["has_audio"] and info["duration"] >= min_len:
                tracks.append(Track(str(p), info["duration"]))
    return tracks


def choose_track(cfg: Config, db: Database, tracks: list[Track], rng: random.Random | None = None) -> Track:
    """Pick a track: never the previous one; avoid last N; prefer least-used."""
    if not tracks:
        raise ValueError("No usable music tracks found (check paths.music_dir)")
    rng = rng or random.Random()
    recent = db.recent_used("song", int(cfg.get("audio.no_repeat_window", 6)))
    window = min(len(recent), max(0, len(tracks) - 1))      # always leave >=1 candidate
    banned = set(recent[:window])
    last = recent[0] if recent else None
    pool = [t for t in tracks if t.path not in banned]
    if len(tracks) > 1 and last:
        pool = [t for t in pool if t.path != last] or [t for t in tracks if t.path != last]
    if not pool:
        pool = tracks
    stats = db.song_stats()
    least = min(stats.get(t.path, 0) for t in pool)
    pool = [t for t in pool if stats.get(t.path, 0) == least]
    return rng.choice(pool)


def choose_start(track: Track, reel_seconds: float, rng: random.Random | None = None) -> float:
    """Random start offset that leaves enough music for the reel (0 if track too short)."""
    rng = rng or random.Random()
    spare = track.duration - reel_seconds - 1.0
    if spare <= 1:
        return 0.0
    return round(rng.uniform(0, spare), 2)
