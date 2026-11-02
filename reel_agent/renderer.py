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
