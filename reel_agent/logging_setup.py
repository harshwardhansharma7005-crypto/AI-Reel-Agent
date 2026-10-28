"""Logging with automatic secret redaction."""
from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

SECRET_ENV = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "IG_ACCESS_TOKEN",
              "S3_SECRET_ACCESS_KEY", "S3_ACCESS_KEY_ID"]
_TOKEN_PARAM = re.compile(r"(access_token=)[^&\s'\"]+", re.I)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for name in SECRET_ENV:
            val = os.environ.get(name, "")
            if val and len(val) > 6:
                msg = msg.replace(val, "***")
        msg = _TOKEN_PARAM.sub(r"\1***", msg)
        record.msg, record.args = msg, ()
        return True


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.addFilter(RedactFilter())
    root.addHandler(sh)
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(Path(log_dir) / "reel_agent.log", maxBytes=2_000_000,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(RedactFilter())
        root.addHandler(fh)
