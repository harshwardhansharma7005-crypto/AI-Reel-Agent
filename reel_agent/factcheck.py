"""Fact verification against OFFICIAL feeds, plus the publish/review decision policy."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import requests

from .analysis import Analysis
from .config import Config

log = logging.getLogger(__name__)
STOP = set("the a an and or of to in on for with is are was were be this that it its as at by from new "
           "now just how what why you your we our can will has have had not but about into more all".split())


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9\.\-]*", (text or "").lower()) if t not in STOP and len(t) > 1}


@dataclass
class FactResult:
    status: str                      # verified | unverified | not_required | unavailable | disabled
    matches: list[dict] = field(default_factory=list)
    notes: str = ""


def default_fetch(url: str) -> str:
    r = requests.get(url, timeout=20, headers={"User-Agent": "AIReelAgent/1.0 (+fact-check)"})
    r.raise_for_status()
    return r.text


def parse_feed(xml_text: str) -> list[dict]:
    """Parse RSS or Atom into [{title, summary, date(datetime|None), link}]."""
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        def txt(name):
            for c in el:
                if c.tag.split("}")[-1] == name:
                    return (c.text or "").strip()
            return ""
        link = txt("link")
        if not link:
            for c in el:
                if c.tag.split("}")[-1] == "link" and c.get("href"):
                    link = c.get("href")
                    break
        raw_date = txt("pubDate") or txt("published") or txt("updated")
        date = None
        if raw_date:
            try:
                date = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError):
                try:
                    date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                except ValueError:
                    date = None
        if date is not None and date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        out.append({"title": txt("title"), "summary": re.sub(r"<[^>]+>", " ", txt("description") or txt("summary")),
                    "date": date, "link": link})
    return out


def verify_analysis(cfg: Config, a: Analysis, fetch: Callable[[str], str] | None = None) -> FactResult:
    if a.claim_type in cfg.get("review.allow_unverified_types", []) and a.claim_type not in \
            cfg.get("review.require_verification_for", []):
        return FactResult("not_required", notes=f"claim type '{a.claim_type}' makes no announcement claim")
    if a.claim_type not in cfg.get("review.require_verification_for", []):
        return FactResult("unverified", notes=f"claim type '{a.claim_type}' is not allowed without verification")
    if not cfg.get("factcheck.enabled", True):
        return FactResult("disabled", notes="fact-check disabled in settings")
    feeds = cfg.get("factcheck.feeds", {}).get(a.topic, [])
    if not feeds:
        return FactResult("unavailable", notes=f"no official feeds configured for topic '{a.topic}'")
    fetch = fetch or default_fetch
    max_age = timedelta(days=int(cfg.get("factcheck.max_age_days", 90)))
    now = datetime.now(timezone.utc)
    claim_sets = [tokens(c) for c in a.claims] or [tokens(a.summary)]
    entity_tok = tokens(a.entity)
    need = float(cfg.get("factcheck.min_match_ratio", 0.5))
    matches, fetched_any = [], False
    for url in feeds:
        try:
            entries = parse_feed(fetch(url))
            fetched_any = fetched_any or bool(entries)
        except Exception as e:
            log.warning("Feed fetch failed (%s): %s", url, e)
            continue
        for e in entries:
            if e["date"] and now - e["date"] > max_age:
                continue
            et = tokens(e["title"] + " " + e["summary"])
            if entity_tok and not (entity_tok & et):
                continue
            for cs in claim_sets:
                if not cs:
                    continue
                ratio = len(cs & et) / len(cs)
                if ratio >= need:
                    matches.append({"title": e["title"], "link": e["link"], "ratio": round(ratio, 2)})
                    break
    if matches:
        return FactResult("verified", matches[:3], "matched official feed entries")
    if not fetched_any:
        return FactResult("unavailable", notes="could not read any official feed")
    return FactResult("unverified", notes="no official entry matched the video's claims")


@dataclass
class Decision:
    publishable: bool
    reason: str = ""
    fact: FactResult | None = None


def decide(cfg: Config, a: Analysis, fact: FactResult) -> Decision:
    """Central policy: when in doubt -> review, never publish."""
    if a.confidence < float(cfg.get("review.min_confidence", 0.6)):
        return Decision(False, f"low confidence ({a.confidence:.2f})", fact)
    if a.claim_type == "unknown":
        return Decision(False, "claim type unknown", fact)
    if fact.status in ("verified", "not_required"):
        return Decision(True, fact.status, fact)
    return Decision(False, f"announcement not verified ({fact.status}: {fact.notes})", fact)
