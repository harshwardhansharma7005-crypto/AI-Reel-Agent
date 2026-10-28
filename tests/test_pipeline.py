import json
import os
import random
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from reel_agent.db import Database
from reel_agent.pipeline import run_pipeline
from reel_agent.slots import resolve_slot
from reel_agent.storage import S3Storage
from tests.helpers import make_cfg
from tests.test_publisher import ENV, FakeResp, FakeSession, FakeStorage, good_script

IST = ZoneInfo("Asia/Kolkata")


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(Path(tempfile.mkdtemp()), copy_media=False)

    def test_auto_slots(self):
        n = lambda h, m: datetime(2026, 10, 29, h, m, tzinfo=IST)  # noqa: E731
        self.assertEqual(resolve_slot(self.cfg, n(10, 7))[1], "2026-10-29-morning")
        self.assertEqual(resolve_slot(self.cfg, n(20, 40))[1], "2026-10-29-evening")   # late GitHub run
        self.assertEqual(resolve_slot(self.cfg, n(9, 55))[0], "morning")
        self.assertEqual(resolve_slot(self.cfg, n(15, 0))[0], "manual")   # nowhere near a slot

    def test_utc_run_time_maps_to_ist_slot(self):
        from datetime import timezone
        utc = datetime(2026, 10, 29, 4, 33, tzinfo=timezone.utc)      # cron 04:30 UTC
        self.assertEqual(resolve_slot(self.cfg, utc)[0], "morning")
        utc = datetime(2026, 10, 29, 14, 31, tzinfo=timezone.utc)
        self.assertEqual(resolve_slot(self.cfg, utc)[0], "evening")

    def test_explicit_slot(self):
        self.assertEqual(resolve_slot(self.cfg, requested="evening")[0], "evening")


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg = make_cfg(cls.tmp, dry_run_tracks_usage=True)
        cls.cfg.set("storage.provider", "local")
        cls.rep = run_pipeline(cls.cfg, dry_run=True, slot="morning", rng=random.Random(5), sync_cloud=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_dry_run_creates_validated_reel_and_metadata(self):
        self.assertEqual(self.rep.status, "dry_run", self.rep.message)
        out = Path(self.rep.output_path)
        self.assertTrue(out.exists() and out.stat().st_size > 10_000)
        meta = json.loads((out.parent / "metadata.json").read_text(encoding="utf-8"))
        self.assertTrue(all(meta["validation"].values()))
        self.assertTrue(meta["dry_run"])
        self.assertIn("#", meta["caption"])

    def test_announcement_video_is_held_for_review(self):
        """The 'openai_announces_new_model' sample cannot be verified offline -> must never be picked."""
        cfg = make_cfg(self.tmp / "only_announce", copy_media=False)
        shutil.copy(self.cfg.path("paths.video_dir") / "openai_announces_new_model.mp4", cfg.path("paths.video_dir"))
        shutil.copy(next(self.cfg.path("paths.music_dir").glob("*.mp3")), cfg.path("paths.music_dir"))
        rep = run_pipeline(cfg, dry_run=True, rng=random.Random(1), sync_cloud=False,
                           fetch=lambda u: (_ for _ in ()).throw(OSError("offline")))
        self.assertEqual(rep.status, "no_candidate")
        self.assertTrue(any("not verified" in r for r in rep.review))
        db = Database(cfg.path("project.data_dir") / "history.db")
        self.assertEqual([r["status"] for r in db.all_videos()], ["review"])
        self.assertFalse(list(cfg.path("project.output_dir").glob("*/reel.mp4")))

    def test_dry_run_with_missing_music_fails_cleanly(self):
        cfg = make_cfg(self.tmp / "nomusic", copy_media=False)
        shutil.copy(self.cfg.path("paths.video_dir") / "openai_chatgpt_demo.mp4", cfg.path("paths.video_dir"))
        rep = run_pipeline(cfg, dry_run=True, sync_cloud=False)
        self.assertEqual(rep.status, "failed")
        self.assertIn("music", rep.message.lower())

    def test_no_repetition_across_many_runs(self):
        cfg = make_cfg(self.tmp / "multi")
        videos, songs, hooks, caps = [], [], [], []
        db = Database(cfg.path("project.data_dir") / "history.db")
        rng = random.Random(11)
        for i in range(5):          # 5 usable videos (1 is held for review)
            rep = run_pipeline(cfg, dry_run=True, slot="manual", rng=rng, db=db, sync_cloud=False, fetch=lambda u: "")
            self.assertEqual(rep.status, "dry_run", rep.message)
            videos.append(rep.video)
            songs.append(rep.music)
            hooks.append(rep.headline)
            meta = json.loads((Path(rep.output_path).parent / "metadata.json").read_text(encoding="utf-8"))
            caps.append(meta["caption"])
            # tracking is off for dry-runs by default: simulate a real tracked run
            db.mark_video_used(Path(rep.video).name and next(r["hash"] for r in db.all_videos() if r["path"] == rep.video))
            db.mark_song_used(rep.music)
            db.add_used("hook", rep.headline)
            db.add_used("caption", meta["caption"].split("\n\n#")[0].split("\n\n#")[0])
            db.add_used("topic", meta["topic"])
            db.add_used("template", meta["template"])
        self.assertEqual(len(set(videos)), 5, "a video was reused before the library was exhausted")
        self.assertEqual(len(set(hooks)), 5)
        self.assertEqual(len(set(caps)), 5)
        self.assertTrue(all(a != b for a, b in zip(songs, songs[1:])), "consecutive song repeat")
        db.close()

    def test_dry_run_does_not_consume_library_by_default(self):
        cfg = make_cfg(self.tmp / "nodrytrack", dry_run_tracks_usage=False)
        run_pipeline(cfg, dry_run=True, rng=random.Random(2), sync_cloud=False)
        db = Database(cfg.path("project.data_dir") / "history.db")
        self.assertTrue(all((r["use_count"] or 0) == 0 for r in db.all_videos()))
        db.close()

    def test_real_publish_end_to_end_with_mocked_instagram(self):
        cfg = make_cfg(self.tmp / "pub", publishing__enabled=True, publishing__dry_run=False,
                       music__i_have_rights_to_publish_all_tracks=True)
        from reel_agent.publisher import InstagramClient
        with mock.patch.dict(os.environ, ENV):
            sess = FakeSession(good_script())
            client = InstagramClient(cfg, session=sess, sleep=lambda s: None)
            rep = run_pipeline(cfg, dry_run=False, slot="evening", rng=random.Random(3), storage=FakeStorage(),
                               client=client, sync_cloud=False, fetch=lambda u: "")
            self.assertEqual(rep.status, "published", rep.message)
            self.assertEqual(sess.count("/media_publish"), 1)
            # a second run for the same slot must NOT publish again
            sess2 = FakeSession(good_script())
            rep2 = run_pipeline(cfg, dry_run=False, slot="evening", rng=random.Random(4), storage=FakeStorage(),
                                client=InstagramClient(cfg, session=sess2, sleep=lambda s: None), sync_cloud=False)
            self.assertEqual(rep2.status, "skipped")
            self.assertEqual(sess2.calls, [])
        db = Database(cfg.path("project.data_dir") / "history.db")
        row = db.all_posts()[0]
        self.assertEqual((row["status"], row["media_id"]), ("published", "M1"))
        csv_text = (cfg.root / "history" / "publication_history.csv").read_text(encoding="utf-8")
        self.assertIn("published", csv_text)
        db.close()

    def test_publishing_blocked_without_music_rights_confirmation(self):
        cfg = make_cfg(self.tmp / "norights", publishing__enabled=True, publishing__dry_run=False)
        with mock.patch.dict(os.environ, ENV):
            rep = run_pipeline(cfg, dry_run=False, rng=random.Random(3), storage=FakeStorage(), sync_cloud=False)
        self.assertEqual(rep.status, "blocked")
        self.assertIn("rights", rep.message)


class FakeS3:
    def __init__(self):
        self.objects = {"videos/a.mp4": b"123", "videos/readme.txt": b"x", "music/t.mp3": b"abcd"}
        self.uploaded = {}

    def list_objects_v2(self, Bucket, Prefix, **kw):
        return {"Contents": [{"Key": k, "Size": len(v)} for k, v in self.objects.items() if k.startswith(Prefix)],
                "IsTruncated": False}

    def download_file(self, bucket, key, dest):
        if key not in self.objects:
            raise KeyError(key)
        Path(dest).write_bytes(self.objects[key])

    def upload_file(self, src, bucket, key, ExtraArgs=None):
        self.uploaded[key] = Path(src).read_bytes()

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://signed.example/{Params['Key']}?exp={ExpiresIn}"


class StorageTests(unittest.TestCase):
    def test_s3_sync_state_and_upload(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = make_cfg(tmp, copy_media=False)
        with mock.patch.dict(os.environ, {"S3_BUCKET": "b"}):
            st = S3Storage(cfg, client=FakeS3())
        res = st.sync_down()
        self.assertEqual(res, {"videos_downloaded": 1, "music_downloaded": 1})   # readme.txt ignored
        self.assertEqual((tmp / "videos" / "a.mp4").read_bytes(), b"123")
        self.assertEqual(st.sync_down(), {"videos_downloaded": 0, "music_downloaded": 0})   # no re-download
        db_file = tmp / "data" / "history.db"
        self.assertFalse(st.pull_state(db_file))            # no remote state yet -> fresh start
        db_file.write_bytes(b"dbdata")
        self.assertTrue(st.push_state(db_file))
        self.assertEqual(st.s3.uploaded["state/history.db"], b"dbdata")
        reel = tmp / "reel.mp4"
        reel.write_bytes(b"v")
        key = st.upload_reel(reel)
        self.assertEqual(key, "reels/reel.mp4")
        self.assertTrue(st.public_url(key).startswith("https://signed.example/reels/reel.mp4"))


if __name__ == "__main__":
    unittest.main()
