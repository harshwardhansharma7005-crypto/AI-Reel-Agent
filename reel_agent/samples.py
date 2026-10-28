"""Synthetic SAMPLE media for testing only (not real tech footage, not real phonk music)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .media import ffmpeg_bin

SAMPLE_VIDEOS = [
    ("openai_chatgpt_demo.mp4", "ChatGPT demo", (20, 90, 70)),
    ("google_gemini_workspace_tutorial.mp4", "Gemini in Workspace", (30, 60, 140)),
    ("unity_game_engine_workflow.mp4", "Unity workflow", (110, 50, 120)),
    ("android_kotlin_compose_tips.mp4", "Android Compose tips", (30, 120, 50)),
    ("ai_tools_cursor_agent.mp4", "AI tools: coding agent", (140, 80, 20)),
    ("openai_announces_new_model.mp4", "OpenAI announces new model", (120, 30, 30)),
]


def _font(size: int):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def make_sample_video(path: Path, title: str, color: tuple[int, int, int], seconds: int = 8,
                      size=(1280, 720), fps: int = 24) -> Path:
    """Render a moving-bar title card with a quiet tone via FFmpeg rawvideo pipe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    w, h = size
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps),
           "-i", "-", "-f", "lavfi", "-i", f"sine=frequency={300 + sum(color)}:duration={seconds}",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    font = _font(64)
    base = np.zeros((h, w, 3), np.uint8)
    for y in range(h):
        base[y, :] = [int(c * (0.4 + 0.6 * y / h)) for c in color]
    for i in range(seconds * fps):
        img = Image.fromarray(base.copy())
        d = ImageDraw.Draw(img)
        d.text((60, h // 2 - 40), title, font=font, fill=(255, 255, 255))
        x = int((i / (seconds * fps)) * (w - 200))
        d.rectangle([x, h - 120, x + 200, h - 80], fill=(255, 87, 51))
        p.stdin.write(img.tobytes())
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError("sample video generation failed")
    return path


def make_sample_music(path: Path, freq: int = 55, seconds: int = 30) -> Path:
    """Synthetic bass pulse - a placeholder, NOT phonk. Replace with your own licensed tracks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
           "-af", "tremolo=f=3:d=0.8,volume=0.8", "-ar", "44100", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def make_sample_library(root: Path, n_videos: int = 6, n_tracks: int = 3) -> tuple[Path, Path]:
    vdir, mdir = root / "videos", root / "music"
    for name, title, color in SAMPLE_VIDEOS[:n_videos]:
        if not (vdir / name).exists():
            make_sample_video(vdir / name, title, color)
    for i in range(n_tracks):
        t = mdir / f"sample_track_{i + 1}.mp3"
        if not t.exists():
            make_sample_music(t, 45 + 10 * i)
    return vdir, mdir
