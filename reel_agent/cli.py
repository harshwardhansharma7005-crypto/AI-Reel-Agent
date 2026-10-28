"""Command line interface:  python -m reel_agent <command>"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import ConfigError, load_config, require_valid, validate_config
from .logging_setup import setup_logging


def _cfg(args):
    cfg = load_config(args.config)
    setup_logging(cfg.path("project.log_dir"))
    return cfg


def cmd_check(args) -> int:
    from .media import MediaError, ffmpeg_bin, ffprobe_bin
    cfg = load_config(args.config)
    errs = validate_config(cfg)
    print("settings.yaml:", "OK" if not errs else "PROBLEMS")
    for e in errs:
        print("  -", e)
    for name, fn in (("ffmpeg", ffmpeg_bin), ("ffprobe", ffprobe_bin)):
        try:
            print(f"{name}: {fn()}")
        except MediaError as e:
            print(f"{name}: MISSING - {e}")
            errs.append(name)
    from .ocr import ocr_available
    print("OCR (tesseract):", "available" if ocr_available(cfg) else "not available (optional)")
    print("video_dir:", cfg.path("paths.video_dir"), "| exists:", cfg.path("paths.video_dir").exists())
    print("music_dir:", cfg.path("paths.music_dir"), "| exists:", cfg.path("paths.music_dir").exists())
    prov = cfg.get("ai.provider")
    if prov == "heuristic":
        print("AI provider: heuristic  ** HEURISTIC MODE: no vision AI; headlines come from file names or go to review **")
    else:
        print(f"AI provider: {prov} (model: {cfg.get(f'ai.models.{prov}', '-')})")
    if prov in ("openai", "anthropic", "gemini"):
        from .config import secret
        env = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}[prov]
        print(f"{env}:", "set" if secret(env) else "MISSING - add it to .env")
    from .publisher import publishing_blockers
    b = publishing_blockers(cfg, dry_run=False)
    print("Publishing:", "READY" if not b else "disabled -> " + "; ".join(b))
    return 1 if errs else 0


def cmd_scan(args) -> int:
    from .pipeline import open_db
    from .scanner import scan_videos
    cfg = _cfg(args)
    db = open_db(cfg)
    r = scan_videos(cfg, db)
    print(f"usable={r.total_usable} new={len(r.new)} duplicates={len(r.duplicates)} skipped={len(r.skipped)}")
    for p, d in r.duplicates:
        print(f"  DUPLICATE {Path(p).name} ~ {Path(d).name}")
    for p, why in r.skipped:
        print(f"  SKIPPED   {Path(p).name}: {why}")
    return 0


def cmd_analyze(args) -> int:
    from .ai.base import get_provider
    from .analysis import analyze_video
    from .factcheck import decide, verify_analysis
    from .pipeline import open_db
    from .scanner import scan_videos
    cfg = _cfg(args)
    db = open_db(cfg)
    scan_videos(cfg, db)
    provider = get_provider(cfg)
    for row in db.all_videos():
        if row["status"] == "duplicate":
            continue
        a = analyze_video(cfg, provider, row["path"], video_hash=row["hash"], use_cache=not args.refresh)
        if a.error:
            print(f"{Path(row['path']).name:45} AI ERROR (not saved, will retry): {a.error}")
            continue
        db.save_analysis(row["hash"], a.topic, a.to_dict())
        dec = decide(cfg, a, verify_analysis(cfg, a))
        if not dec.publishable and row["status"] != "approved":
            db.set_video_status(row["hash"], "review", dec.reason)
        mode = "AI:" + a.source if a.source != "heuristic" else "HEURISTIC"
        print(f"{Path(row['path']).name:45} [{mode}] topic={a.topic:10} type={a.claim_type:12} conf={a.confidence:.2f} "
              f"-> {'OK' if dec.publishable else 'REVIEW: ' + dec.reason}")
        if a.subject:
            print(f"{'':45} subject: {a.subject}")
    return 0


def cmd_run(args) -> int:
    from .pipeline import run_pipeline
    cfg = _cfg(args)
    if args.publish:
        cfg.set("publishing.dry_run", False)
    dry = not args.publish
    rep = run_pipeline(cfg, dry_run=dry, slot=args.slot, force_video=args.video)
    print(json.dumps({k: v for k, v in rep.__dict__.items() if k != "publish"} |
                     ({"publish": rep.publish.__dict__} if rep.publish else {}), indent=2, default=str))
    return 0 if rep.status in ("ready", "dry_run", "published", "skipped") else 2


def cmd_demo(args) -> int:
    """Generate synthetic sample media in media_demo/ and render one sample Reel (never publishes)."""
    from .pipeline import run_pipeline
    from .samples import make_sample_library
    cfg = load_config(args.config)
    root = cfg.root / "media_demo"
    vdir, mdir = make_sample_library(root)
    cfg.set("paths.video_dir", str(vdir))
    cfg.set("paths.music_dir", str(mdir))
    cfg.set("project.data_dir", "media_demo/data")
    cfg.set("project.output_dir", "media_demo/output")
    cfg.set("project.log_dir", "media_demo/logs")
    cfg.set("publishing.enabled", False)
    cfg.set("ai.provider", "heuristic")
    setup_logging(cfg.path("project.log_dir"))
    rep = run_pipeline(cfg, dry_run=True, slot="manual", sync_cloud=False)
    print(f"Status: {rep.status}\nHeadline: {rep.headline}\nOutput: {rep.output_path}")
    for n in rep.review:
        print("  review:", n)
    return 0 if rep.status == "dry_run" else 2


def cmd_review(args) -> int:
    from .pipeline import open_db
    cfg = _cfg(args)
    db = open_db(cfg)
    if args.action == "list":
        rows = [r for r in db.all_videos() if r["status"] == "review"]
        if not rows:
            print("Nothing waiting for review.")
        for r in rows:
            print(f"{r['hash'][:10]}  {Path(r['path']).name}  topic={r['topic']}  reason={r['review_reason']}")
        return 0
    matches = [r for r in db.all_videos() if r["hash"].startswith(args.hash or "!")]
    if len(matches) != 1:
        print("Give a unique hash prefix from 'review list'.")
        return 1
    db.set_video_status(matches[0]["hash"], "approved" if args.action == "approve" else "rejected", "manual")
    print(f"{args.action}d {Path(matches[0]['path']).name}")
    return 0


def cmd_history(args) -> int:
    from .pipeline import open_db
    cfg = _cfg(args)
    db = open_db(cfg)
    if args.csv:
        n = db.export_history_csv(args.csv)
        print(f"Wrote {n} rows to {args.csv}")
    for r in db.all_posts()[-args.limit:]:
        print(f"#{r['id']} {r['slot_key']:28} {r['status']:10} {r['headline']}  {r['permalink'] or ''}")
    return 0


def cmd_cloud(args) -> int:
    from .storage import get_storage
    cfg = _cfg(args)
    st = get_storage(cfg)
    print(st.sync_down() if st.name != "local" else "storage.provider is local; nothing to sync")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reel_agent", description="AI Reel Agent")
    p.add_argument("--config", default=None, help="path to settings.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="check settings, FFmpeg, folders, publishing readiness").set_defaults(fn=cmd_check)
    sub.add_parser("scan", help="scan video library").set_defaults(fn=cmd_scan)
    an = sub.add_parser("analyze", help="analyse all videos and show review decisions")
    an.add_argument("--refresh", action="store_true", help="ignore the analysis cache (makes new API calls)")
    an.set_defaults(fn=cmd_analyze)
    r = sub.add_parser("run", help="make one Reel (dry-run unless --publish)")
    r.add_argument("--slot", default="auto", help="auto | morning | evening | manual")
    r.add_argument("--publish", action="store_true", help="really publish (also needs publishing.enabled: true)")
    r.add_argument("--video", help="force a specific video file name")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("demo", help="generate synthetic sample media + one sample Reel").set_defaults(fn=cmd_demo)
    rv = sub.add_parser("review", help="videos needing human review")
    rv.add_argument("action", choices=["list", "approve", "reject"])
    rv.add_argument("hash", nargs="?")
    rv.set_defaults(fn=cmd_review)
    h = sub.add_parser("history", help="publication history")
    h.add_argument("--csv")
    h.add_argument("--limit", type=int, default=20)
    h.set_defaults(fn=cmd_history)
    sub.add_parser("cloud-sync", help="download videos/music from cloud storage").set_defaults(fn=cmd_cloud)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # readable errors for beginners
        logging.getLogger("reel_agent").exception("Unhandled error")
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
