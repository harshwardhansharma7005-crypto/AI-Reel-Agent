"""Caption + hashtag generation with uniqueness checks."""
from __future__ import annotations

import difflib
import logging
import random
from dataclasses import dataclass

from .ai.base import AIError, AIProvider
from .analysis import Analysis
from .config import Config

log = logging.getLogger(__name__)
IG_MAX_CAPTION = 2200
IG_MAX_HASHTAGS = 30

BASE_TAGS = ["#technology", "#tech", "#technews", "#reels", "#innovation", "#futuretech", "#instatech"]
TOPIC_TAGS = {
    "OpenAI": ["#openai", "#chatgpt", "#artificialintelligence", "#ai"],
    "Google": ["#google", "#googleai", "#gemini", "#ai"],
    "Unity": ["#unity", "#unity3d", "#gamedev", "#indiedev", "#gamedevelopment"],
    "Android": ["#android", "#androiddev", "#kotlin", "#mobiledev", "#androidapps"],
    "AI Tools": ["#aitools", "#ai", "#artificialintelligence", "#automation", "#productivity"],
    "Other Tech": ["#gadgets", "#coding", "#software"],
}
OPENERS = ["Quick look:", "Worth a watch:", "Tech break:", "Today in tech:", "Take a look:", "Spotted this:"]
BODIES = [
    "{summary}", "Here's a closer look at {entity} in action.", "A short look at what {entity} can do.",
    "{entity} is worth keeping an eye on.", "Sharing a quick {topic} moment from my library.",
]
CTAS = ["Follow {user} for daily tech.", "Save this for later and follow {user}.", "More tech every day on {user}.",
        "Want more {topic} content? Follow {user}.", "Send this to a friend who loves tech."]


@dataclass
class Caption:
    text: str
    hashtags: list[str]

    def full(self) -> str:
        return (self.text.strip() + "\n\n" + " ".join(self.hashtags)).strip()


def pick_hashtags(a: Analysis, rng: random.Random, n: int = 9) -> list[str]:
    topic = TOPIC_TAGS.get(a.topic, TOPIC_TAGS["Other Tech"])
    kw = ["#" + "".join(ch for ch in k.lower() if ch.isalnum()) for k in a.keywords[:3]]
    pool_base = BASE_TAGS[:]
    rng.shuffle(pool_base)
    tags: list[str] = []
    for t in topic + [k for k in kw if len(k) > 3] + pool_base:
        if t not in tags:
            tags.append(t)
    head = tags[:len(topic)]
    rest = tags[len(topic):]
    rng.shuffle(rest)
    return (head + rest)[:min(n, IG_MAX_HASHTAGS)]


def validate_caption(cfg: Config, c: Caption, recent: list[str]) -> list[str]:
    issues = []
    full = c.full()
    if len(full) > IG_MAX_CAPTION:
        issues.append("caption exceeds 2200 characters")
    if len(c.hashtags) > IG_MAX_HASHTAGS:
        issues.append("too many hashtags")
    if len(set(c.hashtags)) != len(c.hashtags):
        issues.append("duplicate hashtags")
    thr = float(cfg.get("dedupe.caption_similarity", 0.75))
    for r in recent:
        if difflib.SequenceMatcher(None, c.text.lower(), r.lower()).ratio() >= thr:
            issues.append("too similar to a recent caption")
            break
    return issues


def generate_caption(cfg: Config, provider: AIProvider, a: Analysis, headline: str, recent: list[str],
                     rng: random.Random | None = None) -> Caption:
    rng = rng or random.Random()
    user = cfg.get("reel.username", "@har.*shhh")
    tags = pick_hashtags(a, rng)
    candidates: list[str] = []
    if provider.is_llm:
        try:
            d = provider.complete_json(
                "You write concise, factual Instagram captions for tech Reels. Use only the provided facts. "
                "No emoji spam, no clickbait, no invented news. Reply with one JSON object.",
                f"Headline: {headline}\nTopic: {a.topic}\nSummary: {a.summary}\nClaims: {a.claims}\n"
                f"Write 3 different captions (2-3 short sentences, ending with a call to follow {user}). "
                'JSON: {"captions": ["..."]}')
            candidates += [str(x) for x in d.get("captions", []) if x]
        except AIError as e:
            log.warning("AI caption generation failed: %s", e)
    for _ in range(12):
        opener, body, cta = rng.choice(OPENERS), rng.choice(BODIES), rng.choice(CTAS)
        candidates.append(f"{opener} {headline}.\n\n{body} {cta}".format(
            summary=a.summary or a.topic, entity=a.entity or a.topic, topic=a.topic, user=user))
    for text in candidates:
        c = Caption(text.strip(), tags)
        if not validate_caption(cfg, c, recent):
            return c
    # last resort: guarantee uniqueness by adding the headline hash variant
    return Caption(f"{headline}. {rng.choice(CTAS).format(user=user, topic=a.topic)}", tags)
