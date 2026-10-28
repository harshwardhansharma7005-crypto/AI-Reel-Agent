"""Per-video analysis cache so reruns never repeat (paid) AI calls.

One small JSON file per video + analysis settings under `<data_dir>/analysis_cache/`.
The key covers the video's content hash, provider, model, prompt version, frame settings and transcription mode,
so changing any of them triggers a fresh analysis while plain reruns are free.
Failed or errored analyses are never cached.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


class AnalysisCache:
    def __init__(self, cfg: Config):
        self.enabled = bool(cfg.get("analysis.cache", True))
        self.dir = cfg.path("project.data_dir") / "analysis_cache"

    @staticmethod
    def make_key(cfg: Config, provider_name: str, prompt_version: str) -> str:
        parts = [provider_name, str(cfg.get(f"ai.models.{provider_name}", "")), prompt_version,
                 str(cfg.get("analysis.frame_count", 6)), str(cfg.get("analysis.max_frame_width", 768)),
                 str(cfg.get("analysis.transcribe", "auto")), str(cfg.get("analysis.ocr", "auto")),
                 ",".join(cfg.get("ai.topics", []))]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]

    def _path(self, video_hash: str, key: str) -> Path:
        return self.dir / f"{video_hash[:32]}-{key}.json"

    def get(self, video_hash: str, key: str) -> dict | None:
        if not self.enabled or not video_hash:
            return None
        p = self._path(video_hash, key)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, video_hash: str, key: str, analysis: dict) -> None:
        if not self.enabled or not video_hash:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._path(video_hash, key).write_text(json.dumps(analysis, ensure_ascii=False, indent=1),
                                                   encoding="utf-8")
        except OSError as e:  # pragma: no cover
            log.warning("Could not write analysis cache: %s", e)
