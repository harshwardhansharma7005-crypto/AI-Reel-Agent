"""AI provider abstraction. All HTTP providers use plain `requests` (no SDK needed)."""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from ..config import Config, secret

log = logging.getLogger(__name__)


class AIError(Exception):
    pass


def extract_json(text: str) -> dict:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise AIError("Model did not return JSON")
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise AIError(f"Model returned invalid JSON: {e}") from e


class AIProvider:
    name = "base"
    is_llm = True

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.timeout = int(cfg.get("ai.timeout_seconds", 90))

    def complete_json(self, system: str, prompt: str, images_b64: list[str] | None = None) -> dict:
        raise NotImplementedError

    def transcribe(self, wav_path):  # optional
        return None

    def _post(self, url: str, headers: dict, payload: dict | None = None, files=None, data=None) -> dict:
        last = None
        for attempt in range(3):
            try:
                if files is not None:
                    r = requests.post(url, headers=headers, files=files, data=data, timeout=self.timeout)
                else:
                    r = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                last = f"network error: {e}"
            else:
                if r.status_code < 300:
                    return r.json()
                last = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code in (401, 403):
                    last += " (API key rejected or not allowed - check the key in .env)"
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
            time.sleep(2 ** attempt)
        raise AIError(f"{self.name} request failed: {last}")


class HeuristicProvider(AIProvider):
    """Offline fallback. Uses keywords only; low confidence; cannot read images."""
    name = "heuristic"
    is_llm = False

    def complete_json(self, system, prompt, images_b64=None):
        raise AIError("Heuristic provider cannot generate free-form JSON")


def get_provider(cfg: Config) -> AIProvider:
    name = cfg.get("ai.provider", "heuristic")
    if name == "heuristic":
        return HeuristicProvider(cfg)
    from .providers import AnthropicProvider, GeminiProvider, OpenAIProvider
    table = {"openai": OpenAIProvider, "anthropic": AnthropicProvider, "gemini": GeminiProvider}
    if name not in table:
        raise AIError(f"Unknown AI provider: {name}")
    env = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}[name]
    if not secret(env):
        raise AIError(f"{env} is not set. Add it to .env (or GitHub Secrets), or set ai.provider: heuristic")
    return table[name](cfg)
