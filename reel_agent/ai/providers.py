"""OpenAI, Anthropic and Gemini providers with image (vision) input."""
from __future__ import annotations

import requests

from ..config import secret
from .base import AIError, AIProvider, extract_json


class OpenAIProvider(AIProvider):
    name = "openai"

    def complete_json(self, system, prompt, images_b64=None):
        content = [{"type": "text", "text": prompt}]
        for b in images_b64 or []:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}})
        payload = {"model": self.cfg.get("ai.models.openai", "gpt-4o-mini"),
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                   "response_format": {"type": "json_object"}, "temperature": 0.7}
        data = self._post("https://api.openai.com/v1/chat/completions",
                          {"Authorization": f"Bearer {secret('OPENAI_API_KEY')}"}, payload)
        try:
            return extract_json(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError) as e:
            raise AIError("Unexpected OpenAI response shape") from e

    def transcribe(self, wav_path):
        with open(wav_path, "rb") as f:
            data = self._post("https://api.openai.com/v1/audio/transcriptions",
                              {"Authorization": f"Bearer {secret('OPENAI_API_KEY')}"},
                              files={"file": ("audio.wav", f, "audio/wav")}, data={"model": "whisper-1"})
        return data.get("text", "")


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def complete_json(self, system, prompt, images_b64=None):
        content = []
        for b in images_b64 or []:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}})
        content.append({"type": "text", "text": prompt})
        payload = {"model": self.cfg.get("ai.models.anthropic", "claude-sonnet-5-5"), "max_tokens": 1500,
                   "system": system, "messages": [{"role": "user", "content": content}]}
        data = self._post("https://api.anthropic.com/v1/messages",
                          {"x-api-key": secret("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"}, payload)
        try:
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise AIError("Unexpected Anthropic response shape") from e
        return extract_json(text)


class GeminiProvider(AIProvider):
    name = "gemini"

    def complete_json(self, system, prompt, images_b64=None):
        model = self.cfg.get("ai.models.gemini", "gemini-2.0-flash")
        parts = [{"text": prompt}]
        for b in images_b64 or []:
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b}})
        payload = {"system_instruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": parts}],
                   "generationConfig": {"response_mime_type": "application/json", "temperature": 0.7}}
        data = self._post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          {"x-goog-api-key": secret("GEMINI_API_KEY")}, payload)
        try:
            return extract_json(data["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError) as e:
            raise AIError("Unexpected Gemini response shape") from e
