"""Optional OCR via pytesseract. Gracefully returns '' when unavailable."""
from __future__ import annotations

import logging

import cv2

from .config import Config

log = logging.getLogger(__name__)


def ocr_available(cfg: Config) -> bool:
    if cfg.get("analysis.ocr", "auto") == "off":
        return False
    try:
        import pytesseract
        cmd = cfg.get("analysis.tesseract_cmd", "")
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def ocr_frames(cfg: Config, frames) -> str:
    if not frames or not ocr_available(cfg):
        return ""
    import pytesseract
    seen: list[str] = []
    for fr in frames:
        try:
            gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            text = pytesseract.image_to_string(gray)
        except Exception as e:  # pragma: no cover
            log.warning("OCR failed on a frame: %s", e)
            continue
        for line in text.splitlines():
            line = " ".join(line.split())
            if len(line) >= 3 and line not in seen:
                seen.append(line)
    return "\n".join(seen)[:3000]
