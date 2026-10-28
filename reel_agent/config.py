"""Configuration loading, defaults merging, validation."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent

KNOWN_PROVIDERS = {"heuristic", "openai", "anthropic", "gemini"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class ConfigError(Exception):
    pass


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, data: dict, root: Path | None = None):
        self.data = data
        self.root = Path(root) if root else PROJECT_ROOT

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        cur = self.data
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

    def path(self, key: str) -> Path:
        """Resolve a path-valued setting relative to the project root."""
        raw = self.get(key)
        if raw is None:
            raise ConfigError(f"Missing path setting: {key}")
        p = Path(str(raw)).expanduser()
        return p if p.is_absolute() else (self.root / p)

    def __getitem__(self, path: str):
        v = self.get(path)
        if v is None:
            raise KeyError(path)
        return v


def load_config(path: str | os.PathLike | None = None, overrides: dict | None = None,
                root: Path | None = None, load_env: bool = True) -> Config:
    root = Path(root) if root else PROJECT_ROOT
    if load_env and load_dotenv is not None:
        env_file = root / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)
    cfg_path = Path(path) if path else root / "settings.yaml"
    if not cfg_path.exists():
        raise ConfigError(f"Settings file not found: {cfg_path}")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"settings.yaml is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("settings.yaml must contain a mapping at the top level")
    if overrides:
        data = deep_merge(data, overrides)
    cfg = Config(data, root)
    # environment overrides for common paths / branding
    if os.environ.get("REEL_VIDEO_DIR"):
        cfg.set("paths.video_dir", os.environ["REEL_VIDEO_DIR"])
    if os.environ.get("REEL_MUSIC_DIR"):
        cfg.set("paths.music_dir", os.environ["REEL_MUSIC_DIR"])
    if os.environ.get("REEL_USERNAME"):
        cfg.set("reel.username", os.environ["REEL_USERNAME"])
    return cfg


def validate_config(cfg: Config) -> list[str]:
    """Return a list of human-readable problems (empty list = OK)."""
    errs: list[str] = []
    if cfg.get("reel.width") != 1080 or cfg.get("reel.height") != 1920:
        errs.append("reel.width/height must be 1080x1920 (9:16)")
    slots = cfg.get("schedule.slots", {})
    if not slots:
        errs.append("schedule.slots is empty")
    for name, t in (slots or {}).items():
        if not TIME_RE.match(str(t)):
            errs.append(f"schedule.slots.{name} must be HH:MM, got {t!r}")
    if cfg.get("ai.provider") not in KNOWN_PROVIDERS:
        errs.append(f"ai.provider must be one of {sorted(KNOWN_PROVIDERS)}")
    if not HEX_RE.match(str(cfg.get("headline.highlight_color", ""))):
        errs.append("headline.highlight_color must look like #FF5733")
    if cfg.get("reel.frame_mode") not in ("fixed_16_9", "fit_source"):
        errs.append("reel.frame_mode must be fixed_16_9 or fit_source")
    if cfg.get("reel.min_duration", 0) >= cfg.get("reel.max_duration", 0):
        errs.append("reel.min_duration must be smaller than reel.max_duration")
    if cfg.get("publishing.upload_mode") not in ("url", "resumable"):
        errs.append("publishing.upload_mode must be url or resumable")
    if cfg.get("storage.provider") not in ("local", "s3"):
        errs.append("storage.provider must be local or s3")
    for k in ("audio.music_volume", "audio.original_audio_volume"):
        v = cfg.get(k, 0)
        if not isinstance(v, (int, float)) or not 0 <= v <= 2:
            errs.append(f"{k} must be a number between 0 and 2")
    if not 0 < float(cfg.get("review.min_confidence", 0.6)) <= 1:
        errs.append("review.min_confidence must be in (0, 1]")
    return errs


def require_valid(cfg: Config) -> None:
    errs = validate_config(cfg)
    if errs:
        raise ConfigError("Invalid settings:\n - " + "\n - ".join(errs))


def secret(name: str) -> str:
    return os.environ.get(name, "").strip()
