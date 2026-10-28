"""FFmpeg / FFprobe helpers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


class MediaError(Exception):
    pass


def _find(name: str, env: str) -> str:
    p = os.environ.get(env, "").strip()
    if p and Path(p).exists():
        return p
    found = shutil.which(name)
    if found:
        return found
    raise MediaError(f"{name} not found. Install FFmpeg (Windows: 'winget install Gyan.FFmpeg') and "
                     f"reopen the terminal, or set {env} in .env.")


def ffmpeg_bin() -> str:
    return _find("ffmpeg", "FFMPEG_PATH")


def ffprobe_bin() -> str:
    return _find("ffprobe", "FFPROBE_PATH")


def run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                              errors="replace")
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"Command timed out: {cmd[0]}") from e


def probe(path: str | Path) -> dict:
    """Return normalized media info using ffprobe (display size accounts for rotation)."""
    p = Path(path)
    if not p.exists():
        raise MediaError(f"File not found: {p}")
    r = run([ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
            timeout=60)
    if r.returncode != 0:
        raise MediaError(f"ffprobe failed for {p.name}: {r.stderr.strip()[:300]}")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise MediaError(f"ffprobe returned invalid JSON for {p.name}") from e
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    info = {
        "path": str(p), "size": int(fmt.get("size") or p.stat().st_size),
        "duration": float(fmt.get("duration") or (v or a or {}).get("duration") or 0),
        "has_video": v is not None, "has_audio": a is not None,
        "vcodec": v.get("codec_name") if v else None, "acodec": a.get("codec_name") if a else None,
        "width": 0, "height": 0, "fps": 0.0, "pix_fmt": v.get("pix_fmt") if v else None,
        "profile": v.get("profile") if v else None,
    }
    if v:
        w, h = int(v.get("width", 0)), int(v.get("height", 0))
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        rot = rot or int((v.get("tags") or {}).get("rotate", 0) or 0)
        if abs(rot) % 180 == 90:
            w, h = h, w
        info["width"], info["height"] = w, h
        try:
            n, d = (v.get("avg_frame_rate") or "0/1").split("/")
            info["fps"] = float(n) / float(d) if float(d) else 0.0
        except (ValueError, ZeroDivisionError):
            info["fps"] = 0.0
    return info
