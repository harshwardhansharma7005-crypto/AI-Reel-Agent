import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reel_agent.db import Database
from reel_agent.logging_setup import RedactFilter
from reel_agent.publisher import InstagramClient, publish_reel, publishing_blockers
from tests.helpers import make_cfg
import logging

ENV = {"IG_USER_ID": "1784", "IG_ACCESS_TOKEN": "SECRET_TOKEN_123456"}


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body if body is not None else {}
        self.text = str(self._b)

    def json(self):
        return self._b


class FakeSession:
    """Routes by URL tail. `script` maps key -> list of responses consumed in order."""
    def __init__(self, script):
        self.script, self.calls = script, []

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append((method, url, params, data))
        if url.endswith("content_publishing_limit"):
            key = "limit"
        elif url.endswith("/media_publish"):
            key = "publish"
        elif url.endswith("/media"):
            key = "create"
        elif url.endswith("/C1"):
            key = "status"
        else:
            key = "permalink"
        q = self.script[key]
        return q.pop(0) if len(q) > 1 else q[0]

    def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append(("POST-UPLOAD", url, None, None))
        return FakeResp(200, {"success": True})

    def count(self, tail):
        return sum(1 for c in self.calls if c[1].endswith(tail))


class FakeStorage:
    name = "fake"

    def upload_reel(self, p):
        return "reels/x.mp4"

    def public_url(self, k):
        return "https://cdn.example.com/x.mp4"


def good_script():
    return {"limit": [FakeResp(200, {"data": [{"quota_usage": 1, "config": {"quota_total": 50}}]})],
            "create": [FakeResp(200, {"id": "C1"})],
            "status": [FakeResp(200, {"status_code": "IN_PROGRESS"}), FakeResp(200, {"status_code": "FINISHED"})],
            "publish": [FakeResp(200, {"id": "M1"})],
            "permalink": [FakeResp(200, {"permalink": "https://instagram.com/reel/abc"})]}


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
       
        self.cfg = make_cfg(
            self.tmp,
            copy_media=False,
            publishing__enabled=True,
            publishing__dry_run=False,
            publishing__upload_mode="url",
            music__i_have_rights_to_publish_all_tracks=True,
        )

        self.db = Database(self.tmp / "h.db")
        self.addCleanup(self.db.close)
        self.reel = self.tmp / "r.mp4"
        self.reel.write_bytes(b"x" * 100)
        patcher = mock.patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, slot="S", vh="v"):
        return self.db.create_post(slot_key=slot, video_hash=vh, headline="H", caption="C", status="ready")

    def _client(self, script):
        sess = FakeSession(script)
        return InstagramClient(self.cfg, session=sess, sleep=lambda s: None), sess

    def test_dry_run_never_touches_network(self):
        class Boom:
            def __getattr__(self, n):
                raise AssertionError("network used in dry-run")
        client = InstagramClient(self.cfg, session=Boom())
        r = publish_reel(self.cfg, self.db, self._post(), "s", "v", self.reel, "cap", dry_run=True, client=client)
        self.assertEqual(r.status, "dry_run")

    def test_disabled_by_default_even_with_credentials(self):
        cfg = make_cfg(self.tmp / "d", copy_media=False)     # untouched defaults
        b = publishing_blockers(cfg, dry_run=False)
        self.assertTrue(any("dry-run" in x for x in b))
        self.assertTrue(any("enabled" in x for x in b))
        self.assertTrue(any("rights" in x for x in b))

    def test_missing_credentials_block(self):
        with mock.patch.dict(os.environ, {"IG_USER_ID": "", "IG_ACCESS_TOKEN": ""}):
            b = publishing_blockers(self.cfg, dry_run=False)
        self.assertTrue(any("IG_USER_ID" in x for x in b) and any("IG_ACCESS_TOKEN" in x for x in b))

    def test_full_successful_flow(self):
        client, sess = self._client(good_script())
        pid = self._post("2026-10-29-morning", "vid1")
        r = publish_reel(self.cfg, self.db, pid, "2026-10-29-morning", "vid1", self.reel, "cap", False,
                         FakeStorage(), client)
        self.assertEqual((r.status, r.media_id), ("published", "M1"))
        self.assertEqual(r.permalink, "https://instagram.com/reel/abc")
        post = self.db.get_post(pid)
        self.assertEqual((post["status"], post["container_id"]), ("published", "C1"))
        create = next(c for c in sess.calls if c[1].endswith("/media") and c[0] == "POST")
        self.assertEqual(create[3]["media_type"], "REELS")
        self.assertEqual(create[3]["video_url"], "https://cdn.example.com/x.mp4")
        self.assertEqual(sess.count("/media_publish"), 1)
        self.assertEqual(sess.count("/C1"), 2)       # polled until FINISHED

    def test_same_slot_cannot_publish_twice(self):
        c1, s1 = self._client(good_script())
        publish_reel(self.cfg, self.db, self._post("SLOT", "vid1"), "SLOT", "vid1", self.reel, "cap", False, FakeStorage(), c1)
        c2, s2 = self._client(good_script())
        r = publish_reel(self.cfg, self.db, self._post("SLOT", "vid2"), "SLOT", "vid2", self.reel, "cap", False,
                         FakeStorage(), c2)
        self.assertEqual(r.status, "skipped")
        self.assertEqual(s2.count("/media_publish"), 0)
        self.assertEqual(s2.calls, [])

    def test_same_video_cooldown(self):
        c1, _ = self._client(good_script())
        publish_reel(self.cfg, self.db, self._post("A", "vidX"), "A", "vidX", self.reel, "cap", False, FakeStorage(), c1)
        c2, s2 = self._client(good_script())
        r = publish_reel(self.cfg, self.db, self._post("B", "vidX"), "B", "vidX", self.reel, "cap", False,
                         FakeStorage(), c2)
        self.assertEqual(r.status, "skipped")
        self.assertEqual(s2.count("/media"), 0)

    def test_instagram_processing_error_marks_failed(self):
        sc = good_script()
        sc["status"] = [FakeResp(200, {"status_code": "ERROR", "status": "bad video"})]
        client, sess = self._client(sc)
        pid = self._post()
        r = publish_reel(self.cfg, self.db, pid, "S", "v", self.reel, "cap", False, FakeStorage(), client)
        self.assertEqual(r.status, "failed")
        self.assertEqual(sess.count("/media_publish"), 0)
        self.assertEqual(self.db.get_post(pid)["status"], "failed")

    def test_transient_error_on_create_is_retried(self):
        sc = good_script()
        sc["create"] = [FakeResp(500, {"error": {"code": 2, "message": "temporary"}}), FakeResp(200, {"id": "C1"})]
        client, sess = self._client(sc)
        r = publish_reel(self.cfg, self.db, self._post(), "S", "v", self.reel, "cap", False, FakeStorage(), client)
        self.assertEqual(r.status, "published")
        self.assertEqual(sess.count("/media"), 2)

    def test_permanent_error_not_retried(self):
        sc = good_script()
        sc["create"] = [FakeResp(400, {"error": {"code": 100, "message": "bad param"}})]
        client, sess = self._client(sc)
        r = publish_reel(self.cfg, self.db, self._post(), "S", "v", self.reel, "cap", False, FakeStorage(), client)
        self.assertEqual(r.status, "failed")
        self.assertEqual(sess.count("/media"), 1)

    def test_publish_step_failure_is_never_retried_and_marked_unknown(self):
        sc = good_script()
        sc["publish"] = [FakeResp(500, {"error": {"code": 2, "message": "timeout"}}), FakeResp(200, {"id": "M1"})]
        client, sess = self._client(sc)
        pid = self._post()
        r = publish_reel(self.cfg, self.db, pid, "S", "v", self.reel, "cap", False, FakeStorage(), client)
        self.assertEqual(r.status, "unknown")
        self.assertEqual(sess.count("/media_publish"), 1)       # no duplicate-risk retry
        # and the slot is now blocked until a human checks
        self.assertIsNotNone(self.db.get_post_for_slot("S"))

    def test_quota_exhausted(self):
        sc = good_script()
        sc["limit"] = [FakeResp(200, {"data": [{"quota_usage": 50, "config": {"quota_total": 50}}]})]
        client, sess = self._client(sc)
        r = publish_reel(self.cfg, self.db, self._post(), "S", "v", self.reel, "cap", False, FakeStorage(), client)
        self.assertEqual(r.status, "failed")
        self.assertEqual(sess.count("/media"), 0)

    def test_url_mode_without_storage_fails_cleanly(self):
        client, _ = self._client(good_script())
        r = publish_reel(self.cfg, self.db, self._post(), "S", "v", self.reel, "cap", False, None, client)
        self.assertEqual(r.status, "failed")
        self.assertIn("storage", r.detail.lower())

    def test_resumable_mode_uploads_bytes(self):
        cfg = make_cfg(self.tmp / "r", copy_media=False, publishing__enabled=True, publishing__dry_run=False,
                       publishing__upload_mode="resumable", music__i_have_rights_to_publish_all_tracks=True)
        sess = FakeSession(good_script())
        client = InstagramClient(cfg, session=sess, sleep=lambda s: None)
        r = publish_reel(cfg, self.db, self._post(), "S", "v", self.reel, "cap", False, None, client)
        self.assertEqual(r.status, "published")
        self.assertEqual(sess.count("/C1"), 3)  # upload POST + 2 status polls (all end with /C1)
        create = next(c for c in sess.calls if c[1].endswith("/media") and c[0] == "POST")
        self.assertEqual(create[3]["upload_type"], "resumable")
        self.assertNotIn("video_url", create[3])

    def test_token_is_redacted_from_logs(self):
        rec = logging.LogRecord("x", logging.INFO, "f", 1, "GET https://g/x?access_token=SECRET_TOKEN_123456&a=1 %s",
                                ("SECRET_TOKEN_123456",), None)
        RedactFilter().filter(rec)
        self.assertNotIn("SECRET_TOKEN_123456", rec.getMessage())


if __name__ == "__main__":
    unittest.main()
