"""Shared test helpers. Builds synthetic sample media once per test run."""
from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

from reel_agent.config import PROJECT_ROOT, Config, load_config
from reel_agent.samples import make_sample_library

_LIB: tuple[Path, Path] | None = None
_LIB_ROOT: Path | None = None


def sample_library() -> tuple[Path, Path]:
    """(video_dir, music_dir) - generated once, treat as read-only (copy if you need to modify)."""
    global _LIB, _LIB_ROOT
    if _LIB is None:
        _LIB_ROOT = Path(tempfile.mkdtemp(prefix="reel_samples_"))
        atexit.register(shutil.rmtree, _LIB_ROOT, ignore_errors=True)
        _LIB = make_sample_library(_LIB_ROOT)
    return _LIB


def make_cfg(tmp: Path, copy_media: bool = True, **overrides) -> Config:
    """Config rooted in `tmp` so tests never touch the real project folders."""
    tmp = Path(tmp)
    vdir, mdir = tmp / "videos", tmp / "music"
    if copy_media:
        sv, sm = sample_library()
        shutil.copytree(sv, vdir, dirs_exist_ok=True)
        shutil.copytree(sm, mdir, dirs_exist_ok=True)
    else:
        vdir.mkdir(parents=True, exist_ok=True)
        mdir.mkdir(parents=True, exist_ok=True)
    cfg = load_config(PROJECT_ROOT / "settings.yaml", root=tmp, load_env=False)
    cfg.set("paths.video_dir", str(vdir))
    cfg.set("paths.music_dir", str(mdir))
    cfg.set("project.data_dir", str(tmp / "data"))
    cfg.set("project.output_dir", str(tmp / "output"))
    cfg.set("project.log_dir", str(tmp / "logs"))
    for k, v in overrides.items():
        cfg.set(k.replace("__", "."), v)
    return cfg
