"""SQLite history: videos, songs, used items (hooks/captions/templates), posts."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos(
  hash TEXT PRIMARY KEY, path TEXT, size INTEGER, duration REAL, width INTEGER, height INTEGER,
  phash TEXT, status TEXT DEFAULT 'new', duplicate_of TEXT, topic TEXT, analysis_json TEXT,
  review_reason TEXT, use_count INTEGER DEFAULT 0, last_used TEXT, first_seen TEXT);
CREATE TABLE IF NOT EXISTS songs(
  path TEXT PRIMARY KEY, use_count INTEGER DEFAULT 0, last_used TEXT);
CREATE TABLE IF NOT EXISTS used_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, value TEXT, created TEXT);
CREATE INDEX IF NOT EXISTS idx_used_kind ON used_items(kind, id);
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, slot_key TEXT, video_hash TEXT, music_path TEXT,
  headline TEXT, caption TEXT, output_path TEXT, status TEXT, container_id TEXT,
  media_id TEXT, permalink TEXT, error TEXT, created TEXT, updated TEXT);
CREATE INDEX IF NOT EXISTS idx_posts_slot ON posts(slot_key);
"""

# Posts in these states block another publish for the same slot / video.
BLOCKING_STATES = ("published", "publishing", "unknown")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- videos
    def upsert_video(self, **f: Any) -> None:
        existing = self.get_video(f["hash"])
        if existing:
            self.conn.execute("UPDATE videos SET path=?, size=?, duration=?, width=?, height=? WHERE hash=?",
                              (f["path"], f["size"], f["duration"], f["width"], f["height"], f["hash"]))
        else:
            self.conn.execute(
                "INSERT INTO videos(hash,path,size,duration,width,height,phash,status,duplicate_of,first_seen) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f["hash"], f["path"], f["size"], f["duration"], f["width"], f["height"], f.get("phash"),
                 f.get("status", "new"), f.get("duplicate_of"), now_iso()))
        self.conn.commit()

    def get_video(self, h: str):
        return self.conn.execute("SELECT * FROM videos WHERE hash=?", (h,)).fetchone()

    def all_videos(self):
        return self.conn.execute("SELECT * FROM videos ORDER BY first_seen, path").fetchall()

    def set_video_status(self, h: str, status: str, reason: str | None = None) -> None:
        self.conn.execute("UPDATE videos SET status=?, review_reason=? WHERE hash=?", (status, reason, h))
        self.conn.commit()

    def save_analysis(self, h: str, topic: str, analysis: dict) -> None:
        self.conn.execute("UPDATE videos SET topic=?, analysis_json=? WHERE hash=?",
                          (topic, json.dumps(analysis, ensure_ascii=False), h))
        self.conn.commit()

    def mark_video_used(self, h: str) -> None:
        self.conn.execute("UPDATE videos SET use_count=use_count+1, last_used=? WHERE hash=?", (now_iso(), h))
        self.conn.commit()

    # ---- songs
    def mark_song_used(self, path: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO songs(path) VALUES(?)", (path,))
        self.conn.execute("UPDATE songs SET use_count=use_count+1, last_used=? WHERE path=?", (now_iso(), path))
        self.add_used("song", path)
        self.conn.commit()

    def song_stats(self) -> dict[str, int]:
        return {r["path"]: r["use_count"] for r in self.conn.execute("SELECT path, use_count FROM songs")}

    # ---- generic used items
    def add_used(self, kind: str, value: str) -> None:
        self.conn.execute("INSERT INTO used_items(kind,value,created) VALUES(?,?,?)", (kind, value, now_iso()))
        self.conn.commit()

    def recent_used(self, kind: str, limit: int = 50) -> list[str]:
        rows = self.conn.execute("SELECT value FROM used_items WHERE kind=? ORDER BY id DESC LIMIT ?",
                                 (kind, limit)).fetchall()
        return [r["value"] for r in rows]

    # ---- posts
    def get_post_for_slot(self, slot_key: str):
        return self.conn.execute(
            "SELECT * FROM posts WHERE slot_key=? AND status IN (%s) ORDER BY id DESC LIMIT 1"
            % ",".join("?" * len(BLOCKING_STATES)), (slot_key, *BLOCKING_STATES)).fetchone()

    def recently_published_video(self, video_hash: str, days: int) -> bool:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        r = self.conn.execute(
            "SELECT 1 FROM posts WHERE video_hash=? AND status IN (%s) AND created>=? LIMIT 1"
            % ",".join("?" * len(BLOCKING_STATES)), (video_hash, *BLOCKING_STATES, since)).fetchone()
        return r is not None

    def create_post(self, **f: Any) -> int:
        t = now_iso()
        cur = self.conn.execute(
            "INSERT INTO posts(slot_key,video_hash,music_path,headline,caption,output_path,status,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (f["slot_key"], f.get("video_hash"), f.get("music_path"), f.get("headline"), f.get("caption"),
             f.get("output_path"), f.get("status", "ready"), t, t))
        self.conn.commit()
        return int(cur.lastrowid)

    def update_post(self, post_id: int, **f: Any) -> None:
        allowed = {"status", "container_id", "media_id", "permalink", "error", "output_path"}
        sets = [(k, v) for k, v in f.items() if k in allowed]
        if not sets:
            return
        q = ", ".join(f"{k}=?" for k, _ in sets) + ", updated=?"
        self.conn.execute(f"UPDATE posts SET {q} WHERE id=?", (*[v for _, v in sets], now_iso(), post_id))
        self.conn.commit()

    def get_post(self, post_id: int):
        return self.conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()

    def all_posts(self):
        return self.conn.execute("SELECT * FROM posts ORDER BY id").fetchall()

    def export_history_csv(self, path: str | Path) -> int:
        rows = self.all_posts()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        cols = ["id", "slot_key", "status", "headline", "video_hash", "music_path", "media_id", "permalink",
                "error", "created", "updated"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r[c] for c in cols])
        return len(rows)
