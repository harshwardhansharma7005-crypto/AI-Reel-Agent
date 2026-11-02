"""Reel rendering: Pillow overlay (headline, border, branding) + FFmpeg composition/mixing."""
from __future__ import annotations

import logging
import os
import platform
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .config import Config
from .headline import Headline, split_highlight
from .media import MediaError, ffmpeg_bin, probe, run
from .templates import Template

log = logging.getLogger(__name__)


class RenderError(Exception):
    pass


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _font_dirs(cfg: Config) -> list[Path]:
    dirs = [cfg.root / "assets" / "fonts"]
    sysname = platform.system()
    if sysname == "Windows":
        dirs.append(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sysname == "Darwin":
        dirs += [Path("/Library/Fonts"), Path("/System/Library/Fonts"), Path.home() / "Library" / "Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path.home() / ".fonts", Path.home() / ".local/share/fonts"]
    return [d for d in dirs if d.exists()]


@lru_cache(maxsize=64)
def _find_font(root: str, dirs: tuple[str, ...], names: tuple[str, ...], override: str) -> str | None:
    if override and Path(override).exists():
        return override
    for n in names:
        for d in dirs:
            direct = Path(d) / n
            if direct.exists():
                return str(direct)
    lowered = {n.lower() for n in names}
    for n in names:
        for d in dirs:
            for p in Path(d).rglob("*"):
                if p.is_file() and p.name.lower() == n.lower():
                    return str(p)
    return None


def resolve_font_path(cfg: Config, tpl: Template, kind: str = "headline") -> str | None:
    override = cfg.get(f"reel.fonts.{kind}", "") or ""
    dirs = tuple(str(d) for d in _font_dirs(cfg))
    return _find_font(str(cfg.root), dirs, tpl.fonts, override)


def load_font(path: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            log.warning("Could not load font %s", path)
    log.warning("No TrueType font found - using Pillow default. Put a .ttf in assets/fonts/.")
    try:
        return ImageFont.load_default(size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


@dataclass
class Layout:
    headline_bbox: tuple[int, int, int, int]
    video_rect: tuple[int, int, int, int]       # x, y, w, h
    font_size: int
    lines: list[str]
    template: str
    username_bbox: tuple[int, int, int, int] | None = None


def _wrap(words: list[tuple[str, bool]], font, max_w: int) -> list[list[tuple[str, bool]]] | None:
    space = font.getlength(" ")
    lines: list[list[tuple[str, bool]]] = []
    cur: list[tuple[str, bool]] = []
    cur_w = 0.0
    for w, hl in words:
        ww = font.getlength(w)
        if ww > max_w:
            return None
        add = ww if not cur else space + ww
        if cur and cur_w + add > max_w:
            lines.append(cur)
            cur, cur_w = [(w, hl)], ww
        else:
            cur.append((w, hl))
            cur_w += add
    if cur:
        lines.append(cur)
    return lines


def _balance(words: list[tuple[str, bool]], font, max_w: int, n: int):
    """Re-wrap into at most `n` lines of near-equal width (narrowest width that still fits n lines)."""
    lo, hi = 1, max_w
    best = _wrap(words, font, max_w)
    while lo < hi:
        mid = (lo + hi) // 2
        trial = _wrap(words, font, mid)
        if trial is not None and len(trial) <= n:
            best, hi = trial, mid
        else:
            lo = mid + 1
    return best


def fit_headline(words, tpl: Template, font_path: str | None, max_w: int):
    """Largest font whose balanced wrap fits. Two lines are preferred (orange first line + white second)."""
    for limit in (2, tpl.max_lines):
        size = tpl.font_max
        while size >= tpl.font_min:
            font = load_font(font_path, size)
            lines = _wrap(words, font, max_w)
            if lines is not None and len(lines) <= limit:
                return font, _balance(words, font, max_w, max(2, len(lines)) if len(words) > 1 else 1), size
            size -= 4
    raise RenderError("Headline does not fit the template; shorten it")


def build_overlay(cfg: Config, headline: Headline, tpl: Template, src_w: int, src_h: int) -> tuple[Image.Image, Layout]:
    W, H = int(cfg["reel.width"]), int(cfg["reel.height"])
    mx = int(cfg.get("reel.safe_margin_x", 70))
    top, bottom = int(cfg.get("reel.safe_top", 200)), int(cfg.get("reel.safe_bottom", 380))
    accent = hex_to_rgb(cfg.get("headline.highlight_color", "#FF5733"))
    white = (255, 255, 255)

    img = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    text = headline.text.upper() if tpl.uppercase else headline.text
    hl = [h.upper() for h in headline.highlights] if tpl.uppercase else headline.highlights
    words = split_highlight(text, hl)
    max_w = W - 2 * mx
    font_path = resolve_font_path(cfg, tpl)
    font, lines, size = fit_headline(words, tpl, font_path, max_w)
    ascent, descent = font.getmetrics()
    line_h = int((ascent + descent) * 1.08)
    block_h = line_h * len(lines)
    y0 = top
    space = font.getlength(" ")
    min_x, max_x = W, 0
    for i, ln in enumerate(lines):
        lw = sum(font.getlength(w) for w, _ in ln) + space * (len(ln) - 1)
        x = (W - lw) / 2                      # every headline line is centered horizontally
        y = y0 + i * line_h
        min_x, max_x = min(min_x, x), max(max_x, x + lw)
        for w, _ in ln:                       # first line orange-red, remaining line(s) white
            draw.text((x, y), w, font=font, fill=accent if i == 0 else white)
            x += font.getlength(w) + space
    head_bbox = (int(min_x), y0, int(max_x), y0 + block_h)
    if min_x < mx - 1 or max_x > W - mx + 1:
        raise RenderError("Headline exceeds safe margins")

    # ---- video rectangle
    rw = tpl.video_w
    if cfg.get("reel.frame_mode", "fixed_16_9") == "fixed_16_9":
        rh = rw * 9 // 16
    else:
        rh = int(rw * src_h / max(src_w, 1))
        max_h = 900
        if rh > max_h:
            rw, rh = int(max_h * src_w / max(src_h, 1)), max_h
    rw, rh = rw - rw % 2, rh - rh % 2
    center_y = H // 2 + 40
    gap = 70
    ry = max(head_bbox[3] + gap, center_y - rh // 2)
    if ry + rh > H - bottom:
        ry = H - bottom - rh
        if ry < head_bbox[3] + 20:
            raise RenderError("Layout does not fit: headline too tall for the safe area")
    rx = (W - rw) // 2
    rect = (rx, ry, rw, rh)

    # ---- border / glow (drawn outside the video area)
    pad = 8
    box = [rx - pad, ry - pad, rx + rw + pad, ry + rh + pad]
    if tpl.border == "glow":
        glow = Image.new("RGB", (W, H), (0, 0, 0))
        ImageDraw.Draw(glow).rectangle(box, outline=accent, width=10)
        glow = glow.filter(ImageFilter.GaussianBlur(12))
        dim = glow.point(lambda v: int(v * 0.55))
        img = ImageChops.lighter(img, dim)
        draw = ImageDraw.Draw(img)
        draw.rectangle(box, outline=accent, width=2)
    elif tpl.border == "solid":
        draw.rectangle(box, outline=accent, width=4)
    elif tpl.border == "thin":
        draw.rectangle(box, outline=(110, 110, 110), width=2)
    elif tpl.border == "brackets":
        L, t, off = 80, 8, 14
        x1, y1, x2, y2 = rx - off, ry - off, rx + rw + off, ry + rh + off
        for (cx, cy, dx, dy) in ((x1, y1, 1, 1), (x2, y1, -1, 1), (x1, y2, 1, -1), (x2, y2, -1, -1)):
            draw.rectangle([min(cx, cx + dx * L), min(cy, cy + dy * t), max(cx, cx + dx * L), max(cy, cy + dy * t)],
                           fill=accent)
            draw.rectangle([min(cx, cx + dx * t), min(cy, cy + dy * L), max(cx, cx + dx * t), max(cy, cy + dy * L)],
                           fill=accent)

    # ---- username branding
    user = str(cfg.get("reel.username", "")).strip()
    ubox = None
    if user:
        ufont_path = cfg.get("reel.fonts.username", "") or font_path
        ufont = load_font(ufont_path, 38)
        alpha = int(cfg.get("reel.username_alpha", 190))
        col = (alpha, alpha, alpha)
        uy = min(ry + rh + pad + 36, H - bottom - 20)      # directly below the video frame
        draw.text((W / 2, uy), user, font=ufont, fill=col, anchor="mt")   # centered on the canvas
        ubox = tuple(int(round(v)) for v in draw.textbbox((W / 2, uy), user, font=ufont, anchor="mt"))
    return img, Layout(head_bbox, rect, size, [" ".join(w for w, _ in ln) for ln in lines], tpl.name, ubox)


@dataclass
class RenderResult:
    path: Path
    duration: float
    layout: Layout
    clip_start: float
    music_start: float
    command: list[str] = field(default_factory=list)


def _fnum(x: float) -> str:
    return f"{x:.3f}"


def render_reel(cfg: Config, video_path: str | Path, music_path: str | Path, out_path: str | Path,
                headline: Headline, tpl: Template, music_start: float = 0.0, clip_start: float = 0.0,
                duration: float | None = None, keep_overlay: bool = False) -> RenderResult:
    video_path, music_path, out_path = Path(video_path), Path(music_path), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        vi = probe(video_path)
        mi = probe(music_path)
    except MediaError as e:
        raise RenderError(str(e)) from e
    if not vi["has_video"]:
        raise RenderError("Source has no video stream")
    if not mi["has_audio"]:
        raise RenderError("Music file has no audio stream")
    max_d, min_d = float(cfg["reel.max_duration"]), float(cfg["reel.min_duration"])
    avail = vi["duration"] - clip_start
    D = min(duration or max_d, max_d, avail)
    if D < min_d:
        raise RenderError(f"Usable clip is {D:.1f}s, shorter than reel.min_duration={min_d}s")
    fps = int(cfg.get("reel.fps", 30))
    W, H = int(cfg["reel.width"]), int(cfg["reel.height"])
    overlay, layout = build_overlay(cfg, headline, tpl, vi["width"], vi["height"])
    rx, ry, rw, rh = layout.video_rect
    tmp = Path(tempfile.mkdtemp(prefix="reel_"))
    overlay_path = tmp / "overlay.png"
    overlay.save(overlay_path)

    mv = float(cfg.get("audio.music_volume", 0.85))
    ov = float(cfg.get("audio.original_audio_volume", 0.0))
    fade = min(float(cfg.get("audio.fade_seconds", 1.0)), D / 3)
    use_orig = vi["has_audio"] and ov > 0
    loop_music = mi["duration"] - music_start < D + 0.5

    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
           "-loop", "1", "-framerate", str(fps), "-i", str(overlay_path),
           "-ss", _fnum(clip_start), "-t", _fnum(D), "-i", str(video_path)]
    if loop_music:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-ss", _fnum(music_start if not loop_music else min(music_start, max(0.0, mi["duration"] - 1))),
            "-i", str(music_path)]
    fg = [
        f"[1:v]scale={rw}:{rh}:force_original_aspect_ratio=decrease,"
        f"pad={rw}:{rh}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},format=yuv420p[vid]",
        f"[0:v]format=yuv420p[bg]",
        f"[bg][vid]overlay={rx}:{ry}:shortest=1,format=yuv420p[v]",
        f"[2:a]aresample=44100,volume={mv},afade=t=in:st=0:d={_fnum(fade)},"
        f"afade=t=out:st={_fnum(max(0.0, D - fade))}:d={_fnum(fade)}[m]",
    ]
    tail = "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=44100" if cfg.get("audio.loudnorm", True) else "alimiter=limit=0.95"
    if use_orig:
        fg.append(f"[1:a]aresample=44100,volume={ov}[o]")
        fg.append(f"[m][o]amix=inputs=2:duration=first:dropout_transition=0,volume=2,{tail}[a]")
    else:
        fg.append(f"[m]{tail}[a]")
    cmd += ["-filter_complex", ";".join(fg), "-map", "[v]", "-map", "[a]", "-t", _fnum(D),
            "-c:v", "libx264", "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p", "-preset", "medium",
            "-crf", "20", "-r", str(fps), "-g", str(fps * 2), "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-ac", "2", "-movflags", "+faststart", str(out_path)]
    log.info("Rendering %s (%.1fs, template=%s)", out_path.name, D, tpl.name)
    r = run(cmd, timeout=1800)
    if r.returncode != 0 or not out_path.exists():
        raise RenderError(f"FFmpeg failed: {r.stderr.strip()[-800:]}")
    if not keep_overlay:
        try:
            overlay_path.unlink()
            tmp.rmdir()
        except OSError:
            pass
    return RenderResult(out_path, D, layout, clip_start, music_start, cmd)
