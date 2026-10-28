"""Video understanding: frames + OCR + transcript -> structured Analysis."""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .ai.base import AIError, AIProvider
from .config import Config
from .cache import AnalysisCache
from .frames import extract_representative_frames, frame_to_jpeg_b64
from .ocr import ocr_frames
from .transcribe import transcribe_video

log = logging.getLogger(__name__)

CLAIM_TYPES = ["announcement", "tutorial", "demo", "review", "opinion", "unknown"]

TOPIC_KEYWORDS = {
    "OpenAI": ["openai", "chatgpt", "gpt-4", "gpt4", "gpt-5", "gpt5", "sora", "dall-e", "dalle", "whisper", "codex"],
    "Google": ["google", "gemini", "deepmind", "bard", "pixel", "chrome", "workspace", "notebooklm", "veo"],
    "Unity": ["unity", "unity3d", "unity 6", "c# script", "game engine", "gameobject", "urp", "hdrp"],
    "Android": ["android", "kotlin", "jetpack", "compose", "play store", "apk", "pixel", "samsung galaxy"],
    "Anthropic": ["anthropic", "claude", "claude code", "sonnet", "opus", "haiku"],
    "AI Tools": ["ai tool", "midjourney", "copilot", "cursor", "perplexity", "stable diffusion",
                 "runway", "elevenlabs", "llm", "prompt", "automation", "agent"],
}
ANNOUNCE_WORDS = ["announce", "launch", "released", "release", "introducing", "introduces", "unveil", "just dropped",
                  "now available", "breaking", "new model", "rolling out"]
TUTORIAL_WORDS = ["how to", "tutorial", "step by step", "guide", "learn", "tips", "tip", "workflow", "setup"]

PROMPT_VERSION = "v2"   # bump when prompts change so cached analyses are refreshed

SYSTEM_PROMPT = (
    "You are a careful technology-news video analyst. You are shown frames from a video plus OCR text and an "
    "audio transcript. Report ONLY what is actually visible or audible. Never guess product names, dates, "
    "versions or numbers. If unsure, lower your confidence and say so. Reply with a single JSON object."
)


@dataclass
class Analysis:
    topic: str = "Other Tech"
    entity: str = ""
    claim_type: str = "unknown"
    summary: str = ""
    claims: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    on_screen_text: str = ""
    transcript: str = ""
    confidence: float = 0.0
    concerns: list[str] = field(default_factory=list)
    source: str = "heuristic"       # heuristic | openai | anthropic | gemini
    subject: str = ""               # the concrete thing shown (e.g. "Unity Shader Graph water effect")
    filename: str = ""
    frames_used: int = 0
    error: str = ""                 # set when the AI call failed; such analyses are never cached or used

    def evidence_text(self) -> str:
        return " ".join([self.subject, self.summary, self.on_screen_text, self.transcript, " ".join(self.claims),
                         " ".join(self.keywords), self.entity])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Analysis":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def heuristic_analysis(cfg: Config, filename: str, ocr_text: str, transcript: str) -> Analysis:
    name = re.sub(r"[_\-.]+", " ", Path(filename).stem)
    text = f"{name} {ocr_text} {transcript}".lower()
    scores = {}
    for topic, kws in TOPIC_KEYWORDS.items():
        s = 0
        for kw in kws:
            if kw in text:
                s += 2 if kw in name.lower() else 1
        scores[topic] = s
    best = max(scores, key=lambda t: scores[t])
    top = scores[best]
    if top == 0:
        topic, conf = "Other Tech", 0.3
    else:
        topic = best
        conf = 0.7 if top >= 2 else 0.5
        ties = [t for t, s in scores.items() if s == top]
        if len(ties) > 1:
            conf = min(conf, 0.45)
    if any(w in text for w in ANNOUNCE_WORDS):
        ctype = "announcement"
    elif any(w in text for w in TUTORIAL_WORDS):
        ctype = "tutorial"
    elif top > 0:
        ctype = "demo"
    else:
        ctype = "unknown"
    entity = topic if topic != "Other Tech" else ""
    kws = [kw for kw in TOPIC_KEYWORDS.get(topic, []) if kw in text][:6]
    return Analysis(topic=topic, entity=entity, claim_type=ctype, summary=name.strip().capitalize(),
                    filename=Path(filename).name, keywords=kws, on_screen_text=ocr_text[:800], transcript=transcript[:800],
                    confidence=min(conf, 0.7), source="heuristic",
                    concerns=["HEURISTIC MODE: keyword/filename analysis only (no vision AI); verify manually."])


def _llm_prompt(cfg: Config, filename: str, ocr_text: str, transcript: str) -> str:
    topics = ", ".join(cfg.get("ai.topics", []))
    return (
        f"File name: {filename}\nOCR text from frames:\n{ocr_text or '(none)'}\n\nAudio transcript:\n"
        f"{transcript or '(none)'}\n\nAnalyse the attached frames. Return JSON with keys:\n"
        f'  "topic": one of [{topics}],\n  "entity": main company/product shown (string, "" if unclear),\n'
        f'  "claim_type": one of {CLAIM_TYPES},\n  "subject": the specific thing shown, 3-8 words naming the product/technique and what it does,\n'
        '  "summary": 1-3 factual sentences about what the video actually shows or says,\n'
        '  "claims": list of specific factual claims the video makes (announcements, versions, numbers), may be empty,\n'
        '  "keywords": up to 8 short keywords actually visible/audible,\n'
        '  "on_screen_text": important text you can read in the frames,\n'
        '  "confidence": number 0-1 for how sure you are of topic and summary,\n'
        '  "concerns": list of reasons a human should double-check (empty if none).'
    )


def analyze_video(cfg: Config, provider: AIProvider, video_path: str | Path, video_hash: str | None = None,
                  use_cache: bool = True) -> Analysis:
    """Understand a video. With a vision provider: representative frames + OCR + transcript go to the model.

    Successful AI analyses are cached per video (content hash) + provider/model/prompt settings.
    If the AI call fails, the returned Analysis has `error` set and confidence 0: callers must not publish it,
    and it is not cached, so the next run retries.
    """
    video_path = Path(video_path)
    cache, key = AnalysisCache(cfg), ""
    if provider.is_llm and video_hash and use_cache:
        key = AnalysisCache.make_key(cfg, provider.name, PROMPT_VERSION)
        hit = cache.get(video_hash, key)
        if hit:
            log.info("Analysis cache hit for %s (no API call)", video_path.name)
            return Analysis.from_dict(hit)
    frames = extract_representative_frames(video_path, int(cfg.get("analysis.frame_count", 6)),
                                           int(cfg.get("analysis.max_frame_width", 768)))
    ocr_text = ocr_frames(cfg, frames)
    transcript = transcribe_video(cfg, video_path, provider)
    if not provider.is_llm:
        log.warning("HEURISTIC MODE for %s: no vision AI configured (ai.provider: heuristic)", video_path.name)
        a = heuristic_analysis(cfg, video_path.name, ocr_text, transcript)
        a.frames_used = len(frames)
        return a

    def failed(msg: str) -> Analysis:
        log.error("AI analysis failed for %s: %s", video_path.name, msg)
        a = heuristic_analysis(cfg, video_path.name, ocr_text, transcript)
        a.confidence, a.error, a.source = 0.0, msg, provider.name
        a.concerns.append(f"AI analysis failed: {msg}")
        return a

    if not frames:
        return failed("no frames could be read from the video")
    try:
        imgs = [frame_to_jpeg_b64(f) for f in frames]
        d = provider.complete_json(SYSTEM_PROMPT, _llm_prompt(cfg, video_path.name, ocr_text, transcript), imgs)
    except (AIError, ValueError) as e:
        return failed(str(e))
    topics = cfg.get("ai.topics", [])
    topic = d.get("topic") if d.get("topic") in topics else "Other Tech"
    ctype = d.get("claim_type") if d.get("claim_type") in CLAIM_TYPES else "unknown"
    try:
        conf = max(0.0, min(1.0, float(d.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    lst = lambda v: [str(x) for x in v] if isinstance(v, list) else []  # noqa: E731
    summary, subject = str(d.get("summary", "")).strip(), str(d.get("subject", "")).strip()
    concerns = lst(d.get("concerns"))
    if len(summary.split()) < 4 or not subject:
        conf = min(conf, 0.3)       # the model could not say what the video is about -> never trust it
        concerns.append("Model gave no concrete summary/subject.")
    a = Analysis(topic=topic, entity=str(d.get("entity", "")), claim_type=ctype, summary=summary, subject=subject,
                 claims=lst(d.get("claims")), keywords=lst(d.get("keywords")), filename=video_path.name,
                 on_screen_text=str(d.get("on_screen_text", "")) or ocr_text[:800], transcript=transcript[:800],
                 confidence=conf, concerns=concerns, source=provider.name, frames_used=len(frames))
    if key:
        cache.put(video_hash, key, a.to_dict())
    return a
