import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from reel_agent.analysis import Analysis, heuristic_analysis
from reel_agent.config import PROJECT_ROOT, load_config
from reel_agent.factcheck import decide, parse_feed, verify_analysis


def rss(title, desc="", days_ago=3):
    d = format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))
    return (f"<rss><channel><item><title>{title}</title><description>{desc}</description>"
            f"<pubDate>{d}</pubDate><link>https://example.com/p</link></item></channel></rss>")


class AnalysisAndFactTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(PROJECT_ROOT / "settings.yaml", load_env=False)

    def test_heuristic_topics(self):
        cases = {"openai_chatgpt_demo.mp4": "OpenAI", "unity_game_engine_workflow.mp4": "Unity",
                 "android_kotlin_compose_tips.mp4": "Android", "random_cat_video.mp4": "Other Tech"}
        for fn, topic in cases.items():
            self.assertEqual(heuristic_analysis(self.cfg, fn, "", "").topic, topic, fn)

    def test_unknown_topic_low_confidence(self):
        a = heuristic_analysis(self.cfg, "random_cat_video.mp4", "", "")
        self.assertLess(a.confidence, float(self.cfg.get("review.min_confidence")))
        self.assertFalse(decide(self.cfg, a, verify_analysis(self.cfg, a)).publishable)

    def test_announcement_detected(self):
        a = heuristic_analysis(self.cfg, "openai_announces_new_model.mp4", "", "")
        self.assertEqual(a.claim_type, "announcement")

    def test_demo_needs_no_verification_and_is_publishable(self):
        a = heuristic_analysis(self.cfg, "openai_chatgpt_demo.mp4", "", "")
        f = verify_analysis(self.cfg, a)
        self.assertEqual(f.status, "not_required")
        self.assertTrue(decide(self.cfg, a, f).publishable)

    def test_announcement_verified_by_official_feed(self):
        a = Analysis(topic="OpenAI", entity="OpenAI", claim_type="announcement", confidence=0.9,
                     claims=["OpenAI launches new Sora video model"])
        f = verify_analysis(self.cfg, a, fetch=lambda url: rss("OpenAI launches new Sora video model today"))
        self.assertEqual(f.status, "verified")
        self.assertTrue(decide(self.cfg, a, f).publishable)

    def test_announcement_not_in_feed_goes_to_review(self):
        a = Analysis(topic="OpenAI", entity="OpenAI", claim_type="announcement", confidence=0.9,
                     claims=["OpenAI launches quantum toaster"])
        f = verify_analysis(self.cfg, a, fetch=lambda url: rss("OpenAI publishes safety research update"))
        self.assertEqual(f.status, "unverified")
        d = decide(self.cfg, a, f)
        self.assertFalse(d.publishable)
        self.assertIn("not verified", d.reason)

    def test_old_feed_entries_ignored(self):
        a = Analysis(topic="OpenAI", entity="OpenAI", claim_type="announcement", confidence=0.9,
                     claims=["OpenAI launches new Sora video model"])
        f = verify_analysis(self.cfg, a, fetch=lambda url: rss("OpenAI launches new Sora video model", days_ago=400))
        self.assertNotEqual(f.status, "verified")

    def test_feed_failure_is_never_treated_as_verified(self):
        def boom(url):
            raise OSError("offline")
        a = Analysis(topic="Google", entity="Google", claim_type="announcement", confidence=0.95, claims=["Google ships X"])
        f = verify_analysis(self.cfg, a, fetch=boom)
        self.assertEqual(f.status, "unavailable")
        self.assertFalse(decide(self.cfg, a, f).publishable)

    def test_topic_without_feeds_is_unavailable(self):
        a = Analysis(topic="AI Tools", entity="X", claim_type="announcement", confidence=0.95, claims=["X launched"])
        self.assertEqual(verify_analysis(self.cfg, a, fetch=lambda u: "").status, "unavailable")

    def test_low_confidence_blocks_even_demo(self):
        a = Analysis(topic="Unity", entity="Unity", claim_type="demo", confidence=0.3)
        self.assertFalse(decide(self.cfg, a, verify_analysis(self.cfg, a)).publishable)

    def test_atom_feed_parsing(self):
        atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Android 99 beta</title>'
                '<link href="https://x.test/a"/><updated>2026-10-01T10:00:00Z</updated></entry></feed>')
        e = parse_feed(atom)
        self.assertEqual((e[0]["title"], e[0]["link"]), ("Android 99 beta", "https://x.test/a"))
        self.assertEqual(parse_feed("<<not xml"), [])


if __name__ == "__main__":
    unittest.main()
