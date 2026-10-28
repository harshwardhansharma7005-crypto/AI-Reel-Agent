"""Headline generation, validation and highlight-word selection."""
from __future__ import annotations

import difflib
import logging
import random
import re
from pathlib import Path
from dataclasses import dataclass, field

from .ai.base import AIError, AIProvider
from .analysis import Analysis
from .config import Config

log = logging.getLogger(__name__)
_EMOJI = re.compile("[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f]")

# Phrases that say nothing about the actual video. Extended via settings: headline.generic_phrases
GENERIC_PHRASES = [
    "technology in action", "tech in action", "in action:", "a closer look", "closer look", "quick look",
    "quick tour", "worth knowing", "worth watching", "worth a look", "tech demo", "tech worth", "today's tech",
    "in under a minute", "what it can really do", "what they can really do", "game changer", "game-changer",
    "changes everything", "will blow your mind", "you need to see", "the future of", "next level",
]

TOPIC_ANGLES = {
    "OpenAI": "what the OpenAI/ChatGPT product or model in the video concretely does or produces",
    "Google": "what the Google product/feature (Gemini, Workspace, Android, Cloud...) concretely does",
    "Anthropic": "what Claude / the Anthropic product concretely does or produces in the video",
    "Unity": "the specific Unity technique, tool or result shown (shader, UI, physics, workflow...)",
    "Android": "the specific Android/Kotlin/Compose technique or feature shown",
    "AI Tools": "the specific AI tool and the concrete task or result it shows",
    "Other Tech": "the specific technology, device or technique shown and what it does",
}

_STOP = set("""a an the and or but of to in on for with from by at as is are was were be been being this that these those it
its into over under about how what why who when where which new now just more most very your you our their than then
too can will would could should has have had not no yes up out off all any some one two""".split())
_JUNK_FILE_TOKENS = {"mp4", "mov", "mkv", "final", "copy", "video", "videos", "reel", "reels", "clip", "clips", "img",
                     "vid", "new", "edit", "edited", "export", "screen", "recording", "download", "downloaded", "v1",
                     "v2", "v3", "short", "shorts", "tiktok", "instagram", "whatsapp", "untitled", "test"}


@dataclass
class Headline:
    text: str
    highlights: list[str] = field(default_factory=list)
    source: str = "ai"          # ai | filename (descriptive fallback when AI is unavailable / produced nothing valid)


@dataclass
class ValidationResult:
    ok: bool
    issues: list[str] = field(default_factory=list)


def normalize(s: str) -> str:
    return " ".join((s or "").replace("\n", " ").split())


def validate_headline(cfg: Config, text: str, evidence: str = "", recent: list[str] | None = None) -> ValidationResult:
    issues: list[str] = []
    t = normalize(text)
    words = t.split()
    if not t:
        return ValidationResult(False, ["empty headline"])
    if len(words) > int(cfg.get("headline.max_words", 10)):
        issues.append(f"too many words ({len(words)})")
    if len(words) < int(cfg.get("headline.min_words", 3)):
        issues.append(f"too few words ({len(words)})")
    if len(t) > int(cfg.get("headline.max_chars", 70)):
        issues.append(f"too long ({len(t)} chars)")
    low = t.lower()
    for bad in cfg.get("headline.banned_phrases", []):
        if bad.lower() in low:
            issues.append(f"banned phrase: {bad}")
    if _EMOJI.search(t):
        issues.append("contains emoji/symbols the font cannot render")
    if t.count("!") > 1 or "?!" in t:
        issues.append("excessive punctuation")
    letters = [c for c in t if c.isalpha()]
    if len(letters) > 12 and sum(c.isupper() for c in letters) / len(letters) > 0.85 and len(words) > 5:
        issues.append("shouting (all caps)")
    ev = evidence.lower()
    for num in re.findall(r"\d[\d.,]*", t):
        n = num.strip(".,")
        if n and n not in ev:
            issues.append(f"number '{n}' not found in video evidence")
    for g in GENERIC_PHRASES + list(cfg.get("headline.generic_phrases", []) or []):
        if g.lower() in low:
            issues.append(f"generic phrase: {g!r}")
            break
    if evidence.strip():
        issues += _grounding_issues(t, evidence, float(cfg.get("headline.min_grounded_ratio", 0.5)))
    thr = float(cfg.get("headline.similarity_threshold", 0.8))
    for r in recent or []:
        if difflib.SequenceMatcher(None, low, r.lower()).ratio() >= thr:
            issues.append(f"too similar to recent hook: {r!r}")
            break
    return ValidationResult(not issues, issues)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9#+.'\-]+", text)


def _stem(w: str) -> str:
    w = w.lower().strip(".'-")
    for suf in ("'s", "ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _is_named_token(tok: str, first: bool) -> bool:
    """Brand/product-like tokens: ChatGPT, OpenAI, GPT-5, SDK, C#, iOS - things we must never invent."""
    core = tok.strip(".'-")
    if len(core) < 2:
        return False
    if any(c.isdigit() for c in core):
        return True
    inner_caps = any(c.isupper() for c in core[1:])
    return inner_caps and not (first and core[1:].islower())


def _grounding_issues(headline: str, evidence: str, min_ratio: float = 0.5) -> list[str]:
    """Reject headlines naming things (or using no words) that the video evidence does not support."""
    issues: list[str] = []
    ev_low = evidence.lower()
    ev_stems = {_stem(w) for w in _tokens(evidence)}
    toks = _tokens(headline)
    for i, tok in enumerate(toks):
        if _is_named_token(tok, i == 0):
            core = tok.strip(".'-").lower()
            core = core[:-2] if core.endswith("'s") else core
            if core and core not in ev_low and _stem(core) not in ev_stems:
                issues.append(f"'{tok}' not found in video evidence")
    content = [w for w in toks if len(w) > 3 and w.lower() not in _STOP]
    if content:
        hit = sum(_stem(w) in ev_stems for w in content)
        if hit == 0:
            issues.append("headline shares no specific words with the video evidence")
        elif hit / len(content) < min_ratio:
            issues.append(f"only {hit}/{len(content)} headline words are supported by the video evidence")
    return issues


def auto_highlights(text: str, entity: str = "", max_n: int = 2) -> list[str]:
    words = [w.strip(".,:;!?\"'()") for w in normalize(text).split()]
    words = [w for w in words if w]
    chosen: list[str] = []
    ent = {w.lower() for w in entity.split()}
    for w in words:
        if (w.lower() in ent or any(ch.isdigit() for ch in w)) and w not in chosen:
            chosen.append(w)
    for i, w in enumerate(words):
        if len(chosen) >= max_n:
            break
        if i > 0 and w[:1].isupper() and w not in chosen:
            chosen.append(w)
    if not chosen:
        longest = sorted((w for w in words if len(w) > 4), key=len, reverse=True)
        chosen = longest[:1] or words[-1:]
    if len(chosen) >= len(words) and len(words) > 1:
        chosen = chosen[:-1]
    return chosen[:max(max_n, 1)]


def split_highlight(text: str, highlights: list[str]) -> list[tuple[str, bool]]:
    hl = {h.lower().strip(".,:;!?\"'()") for h in highlights}
    out = []
    for w in normalize(text).split():
        out.append((w, w.lower().strip(".,:;!?\"'()") in hl))
    return out


def _llm_headlines(cfg: Config, provider: AIProvider, a: Analysis, n: int, avoid: list[str]) -> list[Headline]:
    system = ("You write specific, catchy headlines for technology Instagram Reels. You may ONLY use facts present in "
              "the provided evidence about the video. Never invent announcements, features, numbers, dates, versions, "
              "results or product names. No emoji, no clickbait, no vague filler. Reply with one JSON object.")
    angle = TOPIC_ANGLES.get(a.topic, TOPIC_ANGLES["Other Tech"])
    mx_w, mx_c = cfg.get("headline.max_words", 10), cfg.get("headline.max_chars", 70)
    banned = ", ".join(f'"{g}"' for g in GENERIC_PHRASES[:12])
    prompt = (f"VIDEO EVIDENCE\nTopic: {a.topic}\nEntity: {a.entity}\nSpecific subject: {a.subject}\n"
              f"Video type: {a.claim_type}\nSummary: {a.summary}\nClaims made in the video: {a.claims}\n"
              f"Keywords: {a.keywords}\nOn-screen text: {a.on_screen_text[:500]}\nTranscript: {a.transcript[:500]}\n\n"
              f"TASK\nWrite {n} DIFFERENT headlines for THIS video. Angle for a {a.topic} video: {angle}. "
              f"Each headline must name the concrete subject (what is actually shown) and make a viewer curious about "
              f"it, in {cfg.get('headline.min_words', 3)}-{mx_w} words and at most {mx_c} characters, sentence case "
              f"(only real product names capitalised). It will be shown on two lines. Every product name, number or "
              f"capability must come from the evidence above. Never use generic phrasing like {banned}. "
              f"Do not reuse these recent headlines: {avoid[:8]}. For each choose 1-3 highlight words that appear in "
              'the headline. JSON: {"headlines": [{"text": "...", "highlight": ["word"]}]}')
    d = provider.complete_json(system, prompt)
    out = []
    for h in d.get("headlines", []):
        if isinstance(h, dict) and h.get("text"):
            hl = h.get("highlight") or []
            out.append(Headline(normalize(h["text"]), [str(x) for x in hl] if isinstance(hl, list) else []))
    return out


def filename_headline(a: Analysis) -> Headline | None:
    """Descriptive, honest fallback: the cleaned file name ("unity_game_engine_workflow.mp4" -> "Unity game engine
    workflow"). Returns None when the name says too little (e.g. IMG_2031.mp4) -> the video goes to review."""
    stem = Path(a.filename).stem if a.filename else ""
    words = [w for w in re.split(r"[\s_\-.]+", stem) if w]
    words = [w for w in words if w.lower() not in _JUNK_FILE_TOKENS and not w.isdigit()
             and not re.fullmatch(r"(img|vid|dsc|mov|pxl)\d*", w.lower())]
    if len(words) < 3:
        return None
    words = words[:8]
    words[0] = words[0][:1].upper() + words[0][1:]
    return Headline(" ".join(words), [], source="filename")


def generate_headline(cfg: Config, provider: AIProvider, a: Analysis, recent: list[str],
                      rng: random.Random | None = None) -> tuple[Headline | None, list[str]]:
    """Return (headline or None, log of rejection reasons). None means: send the video to review.

    AI mode: headlines are written from the analysed content and must pass validation + grounding checks.
    Heuristic mode (or nothing valid from the AI): a descriptive file-name headline, marked source='filename'.
    There are no canned generic headlines."""
    candidates: list[Headline] = []
    log_msgs: list[str] = []
    ai_ok = provider.is_llm and a.source != "heuristic" and not a.error
    if ai_ok:
        try:
            candidates += _llm_headlines(cfg, provider, a, 5, recent)
        except AIError as e:
            log_msgs.append(f"AI headline generation failed: {e}")
    elif not provider.is_llm:
        log_msgs.append("HEURISTIC MODE: no vision AI; using file-name headline")
    evidence = a.evidence_text() + " " + a.filename.replace("_", " ").replace("-", " ")
    for c in candidates:
        v = validate_headline(cfg, c.text, evidence, recent)
        if v.ok:
            c.highlights = [h for h in c.highlights if h.lower() in c.text.lower()] or auto_highlights(c.text, a.entity)
            return c, log_msgs
        log_msgs.append(f"rejected {c.text!r}: {'; '.join(v.issues)}")
    if cfg.get("headline.filename_fallback", True):
        fb = filename_headline(a)
        if fb is not None:
            v = validate_headline(cfg, fb.text, evidence, recent)
            if v.ok:
                fb.highlights = auto_highlights(fb.text, a.entity)
                log_msgs.append(f"using file-name headline {fb.text!r} (no AI headline available)")
                return fb, log_msgs
            log_msgs.append(f"rejected file-name headline {fb.text!r}: {'; '.join(v.issues)}")
    return None, log_msgs
