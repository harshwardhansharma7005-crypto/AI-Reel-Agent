import random
import unittest

from reel_agent.ai.base import HeuristicProvider
from reel_agent.analysis import Analysis
from reel_agent.captions import IG_MAX_HASHTAGS, Caption, generate_caption, validate_caption
from reel_agent.config import PROJECT_ROOT, load_config
from reel_agent.headline import auto_highlights, generate_headline, split_highlight, validate_headline


class HeadlineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(PROJECT_ROOT / "settings.yaml", load_env=False)

    def test_good_headline_passes(self):
        self.assertTrue(validate_headline(self.cfg, "Unity Shader Graph builds a water effect").ok)

    def test_rejections(self):
        cases = {
            "": "empty", "Too short": "too few",
            "one two three four five six seven eight nine ten eleven": "too many words",
            "This guaranteed trick will change coding": "banned",
            "Unity is fire \U0001F525 right now": "emoji",
            "WOW!! Big news!!": "punctuation",
        }
        for text, needle in cases.items():
            r = validate_headline(self.cfg, text)
            self.assertFalse(r.ok, text)
            self.assertIn(needle, " ".join(r.issues).lower(), text)

    def test_numbers_must_appear_in_evidence(self):
        bad = validate_headline(self.cfg, "OpenAI ships 5 new models today", evidence="chatgpt demo")
        self.assertFalse(bad.ok)
        good = validate_headline(self.cfg, "OpenAI ships 5 new models today", evidence="OpenAI shows 5 new models")
        self.assertTrue(good.ok)

    def test_similarity_to_recent_hooks_rejected(self):
        r = validate_headline(self.cfg, "Unity Shader Graph builds a water effect", recent=["Unity Shader Graph builds a water effect"])
        self.assertFalse(r.ok)

    def test_highlighting(self):
        parts = split_highlight("Inside the OpenAI AI toolkit", ["OpenAI"])
        self.assertEqual([w for w, h in parts if h], ["OpenAI"])
        hl = auto_highlights("Inside the OpenAI AI toolkit", "OpenAI")
        self.assertIn("OpenAI", hl)
        self.assertLess(len(hl), 5)

    def test_generate_in_heuristic_mode_is_filename_based_never_generic(self):
        from reel_agent.analysis import heuristic_analysis
        a = heuristic_analysis(self.cfg, "unity_game_engine_workflow.mp4", "", "")
        h, msgs = generate_headline(self.cfg, HeuristicProvider(self.cfg), a, [], random.Random(1))
        self.assertEqual(h.text, "Unity game engine workflow")
        self.assertEqual(h.source, "filename")
        self.assertTrue(any("HEURISTIC" in m for m in msgs))
        self.assertTrue(h.highlights)

    def test_unreadable_filename_goes_to_review(self):
        from reel_agent.analysis import heuristic_analysis
        a = heuristic_analysis(self.cfg, "IMG_2031.mp4", "", "")
        h, _ = generate_headline(self.cfg, HeuristicProvider(self.cfg), a, [], random.Random(1))
        self.assertIsNone(h)

    def test_captions_unique_and_within_limits(self):
        a = Analysis(topic="Android", entity="Android", summary="Compose tips", keywords=["compose"], confidence=0.7)
        prov = HeuristicProvider(self.cfg)
        recent = []
        for i in range(8):
            c = generate_caption(self.cfg, prov, a, "Android tips worth knowing", recent, random.Random(i))
            self.assertEqual(validate_caption(self.cfg, c, recent), [])
            self.assertLessEqual(len(c.hashtags), IG_MAX_HASHTAGS)
            self.assertLessEqual(len(c.full()), 2200)
            self.assertIn("#android", c.hashtags)
            recent.insert(0, c.text)
        self.assertEqual(len(set(recent)), 8)

    def test_caption_validation_flags_duplicates(self):
        c = Caption("same text", ["#a", "#a"])
        issues = validate_caption(self.cfg, c, ["same text"])
        self.assertTrue(any("similar" in i for i in issues))
        self.assertTrue(any("duplicate" in i for i in issues))


if __name__ == "__main__":
    unittest.main()
