"""Optional audio transcription (local faster-whisper or provider API)."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from .config import Config
from .media import MediaError, ffmpeg_bin, run

log = logging.getLogger(__name__)


def extract_audio_wav(video: Path, seconds: int = 90) -> Path | None:
    out = Path(tempfile.mkdtemp()) / "audio.wav"
    r = run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(video), "-t", str(seconds), "-vn", "-ac", "1",
             "-ar", "16000", str(out)], timeout=120)
    return out if r.returncode == 0 and out.exists() and out.stat().st_size > 1000 else None


def transcribe_video(cfg: Config, video: Path, provider=None) -> str:
    mode = cfg.get("analysis.transcribe", "auto")
    if mode == "off":
        return ""
    try:
        wav = extract_audio_wav(video)
    except MediaError:
        return ""
    if wav is None:  # no audio stream
        return ""
    text = ""
    if mode in ("auto", "local"):
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel(cfg.get("analysis.whisper_model", "base"), compute_type="int8")
            segs, _ = model.transcribe(str(wav))
            text = " ".join(s.text.strip() for s in segs)
        except ImportError:
            if mode == "local":
                log.warning("faster-whisper not installed; skipping local transcription")
        except Exception as e:  # pragma: no cover
            log.warning("Local transcription failed: %s", e)
    if not text and mode in ("auto", "openai") and provider is not None and hasattr(provider, "transcribe"):
        try:
            text = provider.transcribe(wav) or ""
        except Exception as e:
            log.warning("Provider transcription failed: %s", e)
    return text.strip()[:4000]
