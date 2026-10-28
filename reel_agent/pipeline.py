"""End-to-end pipeline: scan -> choose -> analyse -> verify -> write -> render -> validate -> publish."""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

from .ai.base import AIProvider, get_provider
from .analysis import Analysis, analyze_video
from .captions import generate_caption
from .config import Config, require_valid
from .db import Database
from .factcheck import decide, verify_analysis
from .headline import generate_headline
from .music import choose_start, choose_track, scan_music
from .publisher import PublishResult, publish_reel
from .renderer import RenderError, render_reel
from .scanner import scan_videos
from .slots import resolve_slot
from .storage import Storage, get_storage
from .templates import choose_template
from .validator import validate_reel

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    slot_key: str
    status: str                       # ready | dry_run | published | skipped | failed | no_candidate | blocked | unknown
    message: str = ""
    output_path: str | None = None
    headline: str | None = None
    video: str | None = None
    music: str | None = None
    publish: PublishResult | None = None
    review: list[str] = field(default_factory=list)
    analysis_mode: str = ""           # "ai:<provider>" or "heuristic"
    headline_source: str = ""         # ai | filename


def open_db(cfg: Config) -> Database:
    return Database(cfg.path("project.data_dir") / "history.db")


def pick_candidates(cfg: Config, db: Database, rng: random.Random) -> list:
    cooldown = int(cfg.get("dedupe.video_cooldown_days", 30))
    last_topics = db.recent_used("topic", 1)
    rows = [r for r in db.all_videos() if r["status"] in ("new", "ok", "approved") and Path(r["path"]).exists()]
    rows = [r for r in rows if not db.recently_published_video(r["hash"], cooldown)]
    rng.shuffle(rows)
    rows.sort(key=lambda r: (r["use_count"] or 0, r["last_used"] or ""))
    if cfg.get("dedupe.avoid_same_topic_twice", True) and last_topics:
        # known-topic videos of the same topic go to the back; unanalysed ones stay in place
        rows.sort(key=lambda r: 1 if r["topic"] == last_topics[0] else 0)
    return rows


def run_pipeline(cfg: Config, *, dry_run: bool = True, slot: str = "auto", provider: AIProvider | None = None,
                 storage: Storage | None = None, rng: random.Random | None = None, db: Database | None = None,
                 now=None, max_attempts: int = 10, client=None, fetch=None, force_video: str | None = None,
                 sync_cloud: bool = True) -> RunReport:
    require_valid(cfg)
    rng = rng or random.Random()
    own_db = db is None
    storage = storage or get_storage(cfg)
    db_path = cfg.path("project.data_dir") / "history.db"
    if own_db and sync_cloud and storage.name != "local":
        storage.pull_state(db_path)
    db = db or open_db(cfg)
    try:
        report = _run(cfg, db, storage, dry_run, slot, provider, rng, now, max_attempts, client, fetch, force_video,
                      sync_cloud)
    finally:
        csv_path = cfg.root / "history" / "publication_history.csv"
        try:
            db.export_history_csv(csv_path)
        except OSError as e:  # pragma: no cover
            log.warning("Could not write history CSV: %s", e)
        if own_db:
            db.close()
            if sync_cloud and storage.name != "local":
                storage.push_state(db_path)
    return report


def _run(cfg, db, storage, dry_run, slot, provider, rng, now, max_attempts, client, fetch, force_video, sync_cloud):
    slot_name, slot_key = resolve_slot(cfg, now, slot)
    log.info("Run started: slot=%s key=%s dry_run=%s", slot_name, slot_key, dry_run)
    if db.get_post_for_slot(slot_key) and not dry_run and slot_name != "manual":
        return RunReport(slot_key, "skipped", "This slot already has a published/pending post")
    if sync_cloud and storage.name != "local":
        log.info("Cloud sync: %s", storage.sync_down())
    provider = provider or get_provider(cfg)
    if not provider.is_llm:
        log.warning("HEURISTIC MODE: ai.provider is 'heuristic' - videos are NOT analysed by a vision AI. "
                    "Headlines fall back to descriptive file names (or review). Set ai.provider + API key in .env.")
    scan = scan_videos(cfg, db)
    log.info("Scan: %d usable, %d new, %d duplicates, %d skipped", scan.total_usable, len(scan.new),
             len(scan.duplicates), len(scan.skipped))
    tracks = scan_music(cfg)
    if not tracks:
        return RunReport(slot_key, "failed", f"No music found in {cfg.path('paths.music_dir')}")
    track_usage = (not dry_run) or bool(cfg.get("dry_run_tracks_usage", False))

    candidates = pick_candidates(cfg, db, rng)
    if force_video:
        candidates = [r for r in db.all_videos() if Path(r["path"]).name == force_video] or candidates
    if not candidates:
        return RunReport(slot_key, "no_candidate", "No eligible videos (all used, duplicates or in review)")
    review_notes: list[str] = []
    for row in candidates[:max_attempts]:
        h, vpath = row["hash"], row["path"]
        name = Path(vpath).name
        a = None
        if row["analysis_json"]:
            stored = Analysis.from_dict(json.loads(row["analysis_json"]))
            # a stored keyword-only/failed analysis must not stand in for a real AI analysis
            if not stored.error and (not provider.is_llm or stored.source == provider.name):
                a = stored
        if a is None:
            a = analyze_video(cfg, provider, vpath, video_hash=h)
            if a.error:     # AI/API problem: do not save, do not mark review - the next run retries
                review_notes.append(f"{name}: AI analysis failed ({a.error}); will retry next run")
                continue
            db.save_analysis(h, a.topic, a.to_dict())
        if row["status"] != "approved":
            fact = verify_analysis(cfg, a, fetch)
            dec = decide(cfg, a, fact)
            if not dec.publishable:
                db.set_video_status(h, "review", dec.reason)
                review_notes.append(f"{name}: {dec.reason}")
                log.warning("Video needs review (%s): %s", name, dec.reason)
                continue
        hl, hl_log = generate_headline(cfg, provider, a, db.recent_used("hook", 80), rng)
        if hl is None:
            db.set_video_status(h, "review", "no valid headline: " + " | ".join(hl_log[-2:]))
            review_notes.append(f"{name}: no valid headline ({' | '.join(hl_log[-2:])})")
            continue
        mode = f"ai:{provider.name}" if provider.is_llm and a.source != "heuristic" else "heuristic"
        if mode == "heuristic":
            log.warning("HEURISTIC MODE headline for %s: %r", name, hl.text)
        track = choose_track(cfg, db, tracks, rng)
        tpl = choose_template(cfg, db.recent_used("template", 6), rng)
        cap = generate_caption(cfg, provider, a, hl.text, db.recent_used("caption", 80), rng)
        out_dir = cfg.path("project.output_dir") / slot_key
        out_path = out_dir / "reel.mp4"
        music_start = choose_start(track, float(cfg["reel.max_duration"]), rng)
        post_id = db.create_post(slot_key=slot_key, video_hash=h, music_path=track.path, headline=hl.text,
                                 caption=cap.full(), output_path=str(out_path), status="rendering")
        try:
            res = render_reel(cfg, vpath, track.path, out_path, hl, tpl, music_start=music_start)
        except RenderError as e:
            db.update_post(post_id, status="failed", error=str(e))
            review_notes.append(f"{name}: render failed: {e}")
            log.error("Render failed for %s: %s", name, e)
            continue
        rep = validate_reel(cfg, out_path, res.duration, res.layout)
        if not rep.ok:
            db.update_post(post_id, status="failed", error="validation: " + "; ".join(rep.errors))
            review_notes.append(f"{name}: validation failed: {rep.errors}")
            log.error("Validation failed: %s", rep.errors)
            continue
        db.update_post(post_id, status="ready")
        meta = {"slot_key": slot_key, "headline": hl.text, "highlights": hl.highlights, "caption": cap.full(),
                "video": vpath, "music": track.path, "music_start": music_start, "template": tpl.name,
                "duration": res.duration, "topic": a.topic, "analysis": a.to_dict(),
                "analysis_mode": mode, "headline_source": hl.source, "validation": rep.checks,
                "dry_run": dry_run}
        (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        if track_usage:
            db.mark_video_used(h)
            db.mark_song_used(track.path)
            db.add_used("hook", hl.text)
            db.add_used("caption", cap.text)
            db.add_used("template", tpl.name)
            db.add_used("topic", a.topic)
        pub = publish_reel(cfg, db, post_id, slot_key, h, out_path, cap.full(), dry_run, storage, client)
        status = {"dry_run": "dry_run", "published": "published", "skipped": "skipped", "failed": "failed",
                  "blocked": "blocked", "unknown": "unknown"}[pub.status]
        msg = pub.detail if mode != "heuristic" else f"[HEURISTIC MODE - not AI-analysed] {pub.detail}"
        return RunReport(slot_key, status, msg, str(out_path), hl.text, vpath, track.path, pub, review_notes,
                         mode, hl.source)
    return RunReport(slot_key, "no_candidate", "All attempted videos were rejected or need review",
                     review=review_notes)
