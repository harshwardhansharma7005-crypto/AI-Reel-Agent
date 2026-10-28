"""AI video analysis, headline relevance, caching, API errors and low-confidence review.

All AI calls here use FAKE providers / mocked HTTP. No real API key was used: these tests prove the plumbing and
the safety rules, NOT the quality of a real model's answers.
"""
import base64
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from reel_agent.ai import providers as prov_mod
from reel_agent.ai.base import AIError, AIProvider, HeuristicProvider
from reel_agent.analysis import Analysis, PROMPT_VERSION, analyze_video
from reel_agent.cache import AnalysisCache
from reel_agent.db import Database
from reel_agent.frames import extract_representative_frames
from reel_agent.headline import Headline, generate_headline, validate_headline
from reel_agent.pipeline import run_pipeline
from tests.helpers import make_cfg, sample_library

# What a vision model would plausibly say about each sample video (file name -> canned model JSON).
CANNED = {
    "openai_chatgpt_demo.mp4": dict(
        topic="OpenAI", entity="ChatGPT", claim_type="demo", subject="ChatGPT writing code from a prompt",
        summary="A screen recording shows ChatGPT turning a short prompt into a working Python function.",
        keywords=["ChatGPT", "Python", "prompt"], confidence=0.9,
        headlines=["ChatGPT turns one prompt into a working Python function", "Watch ChatGPT write Python from a prompt"]),
    "google_gemini_workspace_tutorial.mp4": dict(
        topic="Google", entity="Gemini", claim_type="tutorial", subject="Gemini summarising a Workspace doc",
        summary="A tutorial shows Gemini in Google Workspace summarising a long document into bullet points.",
        keywords=["Gemini", "Workspace", "summary"], confidence=0.85,
        headlines=["Gemini summarises a long Workspace doc in seconds", "Summarise any Google Doc with Gemini"]),
    "claude_code_refactor.mp4": dict(
        topic="Anthropic", entity="Claude", claim_type="demo", subject="Claude Code refactoring a repository",
        summary="A terminal demo shows Claude Code refactoring a small repository and running its tests.",
        keywords=["Claude Code", "refactor", "tests"], confidence=0.88,
        headlines=["Claude Code refactors a repo, then runs its tests", "Refactor a whole repo with Claude Code"]),
    "unity_game_engine_workflow.mp4": dict(
        topic="Unity", entity="Unity", claim_type="tutorial", subject="Unity Shader Graph water effect",
        summary="A tutorial builds a stylised water effect using Unity Shader Graph nodes.",
        keywords=["Unity", "Shader Graph", "water"], confidence=0.9,
        headlines=["Build a water effect in Unity Shader Graph", "Unity Shader Graph: stylised water step by step"]),
    "android_kotlin_compose_tips.mp4": dict(
        topic="Android", entity="Jetpack Compose", claim_type="tutorial", subject="Jetpack Compose list performance tip",
        summary="A tutorial explains how a stable key keeps a Jetpack Compose LazyColumn list from recomposing.",
        keywords=["Compose", "LazyColumn", "key"], confidence=0.8,
        headlines=["Stop LazyColumn recomposing with one stable key", "One Compose key fixes laggy LazyColumn lists"]),
}


class FakeVisionProvider(AIProvider):
    """Stands in for OpenAI/Anthropic/Gemini. Reads the canned answer from the file name in the prompt."""
    name = "fake"

    def __init__(self, cfg, fail=False, confidence=None, headlines=None):
        super().__init__(cfg)
        self.calls, self.fail, self.confidence, self.headlines = [], fail, confidence, headlines

    def _canned(self, prompt):
        for fn, d in CANNED.items():
            if fn in prompt or d["subject"] in prompt:
                return d
        return None

    def complete_json(self, system, prompt, images_b64=None):
        self.calls.append({"prompt": prompt, "images": images_b64})
        if self.fail:
            raise AIError("fake request failed: HTTP 500")
        d = self._canned(prompt)
        if "TASK" in prompt:                       # headline request
            if self.headlines is not None:
                return {"headlines": [{"text": t, "highlight": []} for t in self.headlines]}
            return {"headlines": [{"text": t, "highlight": []} for t in d["headlines"]]}
        if d is None:
            return {"topic": "Other Tech", "summary": "unclear", "confidence": 0.2}
        out = {k: v for k, v in d.items() if k != "headlines"}
        if self.confidence is not None:
            out["confidence"] = self.confidence
        return out

    def n_analysis_calls(self):
        return sum(1 for c in self.calls if c["prompt"].startswith("File name:"))


def _cfg(tmp, **kw):
    return make_cfg(tmp, copy_media=False, analysis__transcribe="off", **kw)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.vdir, cls.mdir = sample_library()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def video(self, name):
        src = self.vdir / name
        if not src.exists():                       # extra topic (Claude) reuses a sample clip under a new name
            src = self.tmp / name
            shutil.copy(self.vdir / "ai_tools_cursor_agent.mp4", src)
        return src


class FrameTests(Base):
    def test_representative_frames_are_distinct_and_counted(self):
        cfg = _cfg(self.tmp / "f")
        frames = extract_representative_frames(self.video("unity_game_engine_workflow.mp4"), 4, 512)
        self.assertEqual(len(frames), 4)
        self.assertTrue(all(f.shape[1] <= 512 for f in frames))

    def test_blank_frames_are_skipped(self):
        out = self.tmp / "blank.mp4"
        w = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 240))
        rng = np.random.default_rng(0)
        for i in range(60):          # first half black, second half noisy colour
            w.write(np.zeros((240, 320, 3), np.uint8) if i < 30 else rng.integers(0, 255, (240, 320, 3), dtype=np.uint8))
        w.release()
        frames = extract_representative_frames(out, 3, 320)
        self.assertTrue(all(f.mean() > 8 for f in frames))

    def test_provider_receives_multiple_valid_jpeg_frames_and_context(self):
        cfg = _cfg(self.tmp / "g", analysis__frame_count=5)
        p = FakeVisionProvider(cfg)
        a = analyze_video(cfg, p, self.video("openai_chatgpt_demo.mp4"))
        call = p.calls[0]
        self.assertEqual(len(call["images"]), 5)
        self.assertEqual(a.frames_used, 5)
        for b64 in call["images"]:
            self.assertEqual(base64.b64decode(b64)[:2], b"\xff\xd8")     # JPEG
        self.assertIn("openai_chatgpt_demo.mp4", call["prompt"])

    def test_transcript_and_ocr_are_sent_to_the_model(self):
        cfg = _cfg(self.tmp / "t")
        p = FakeVisionProvider(cfg)
        with mock.patch("reel_agent.analysis.transcribe_video", return_value="today we build a water shader"), \
                mock.patch("reel_agent.analysis.ocr_frames", return_value="Shader Graph"):
            analyze_video(cfg, p, self.video("unity_game_engine_workflow.mp4"))
        self.assertIn("today we build a water shader", p.calls[0]["prompt"])
        self.assertIn("Shader Graph", p.calls[0]["prompt"])


class TopicHeadlineTests(Base):
    def test_each_topic_gets_a_different_relevant_headline(self):
        cfg = _cfg(self.tmp / "topics")
        seen, recent = {}, []
        for fn, canned in CANNED.items():
            p = FakeVisionProvider(cfg)
            a = analyze_video(cfg, p, self.video(fn))
            a.filename = fn
            self.assertEqual(a.topic, canned["topic"])
            self.assertEqual(a.source, "fake")
            h, msgs = generate_headline(cfg, p, a, recent, random.Random(1))
            self.assertIsNotNone(h, msgs)
            self.assertEqual(h.source, "ai")
            # relevance: shares a specific word with the video's own subject/keywords
            words = {w.lower() for w in (canned["subject"] + " " + " ".join(canned["keywords"])).replace("-", " ").split()}
            self.assertTrue(words & {w.lower().strip(":,") for w in h.text.split()}, h.text)
            self.assertNotIn(h.text, seen.values())
            seen[canned["topic"]] = h.text
            recent.append(h.text)
        self.assertEqual(len(seen), len(CANNED))
        self.assertIn("Claude", seen["Anthropic"])
        self.assertIn("Unity", seen["Unity"])
        self.assertIn("ChatGPT", seen["OpenAI"])

    def test_generic_headlines_are_rejected(self):
        cfg = _cfg(self.tmp / "gen")
        for bad in ("Technology in action", "A closer look at Unity tools", "Unity tips worth knowing",
                    "Today's tech, in under a minute"):
            self.assertFalse(validate_headline(cfg, bad, "Unity tips tools tech").ok, bad)

    def test_invented_claims_are_rejected_then_next_valid_one_used(self):
        cfg = _cfg(self.tmp / "inv")
        p = FakeVisionProvider(cfg, headlines=[
            "OpenAI launches GPT-6 with 10x speed",            # invented product + number
            "ChatGPT now reads your mind",                      # shares only the product name with the evidence
            "ChatGPT turns one prompt into a working Python function"])
        a = analyze_video(cfg, p, self.video("openai_chatgpt_demo.mp4"))
        a.filename = "openai_chatgpt_demo.mp4"
        h, msgs = generate_headline(cfg, p, a, [], random.Random(1))
        self.assertEqual(h.text, "ChatGPT turns one prompt into a working Python function")
        self.assertTrue(any("GPT-6" in m for m in msgs))

    def test_unrelated_headline_rejected_when_nothing_matches(self):
        cfg = _cfg(self.tmp / "unrel")
        p = FakeVisionProvider(cfg, headlines=["Baking sourdough bread at home"])
        a = analyze_video(cfg, p, self.video("unity_game_engine_workflow.mp4"))
        a.filename = "IMG_1.mp4"
        h, msgs = generate_headline(cfg, p, a, [], random.Random(1))
        self.assertIsNone(h)
        self.assertTrue(any("shares no specific words" in m for m in msgs))

    def test_prompt_demands_topic_angle_and_no_invention(self):
        cfg = _cfg(self.tmp / "pr")
        p = FakeVisionProvider(cfg)
        a = analyze_video(cfg, p, self.video("unity_game_engine_workflow.mp4"))
        a.filename = "unity_game_engine_workflow.mp4"
        generate_headline(cfg, p, a, ["old hook"], random.Random(1))
        prompt = [c["prompt"] for c in p.calls if "TASK" in c["prompt"]][0]
        self.assertIn("Shader Graph", prompt)
        self.assertIn("Unity technique", prompt)
        self.assertIn("old hook", prompt)
        self.assertIn("must come from the evidence", prompt)


class ApiErrorTests(Base):
    def test_ai_failure_is_flagged_not_trusted_and_not_cached(self):
        cfg = _cfg(self.tmp / "err")
        p = FakeVisionProvider(cfg, fail=True)
        a = analyze_video(cfg, p, self.video("openai_chatgpt_demo.mp4"), video_hash="abc123")
        self.assertIn("HTTP 500", a.error)
        self.assertEqual(a.confidence, 0.0)
        self.assertFalse(list((cfg.path("project.data_dir") / "analysis_cache").glob("*.json")))
        h, _ = generate_headline(cfg, p, a, [], random.Random(1))
        self.assertTrue(h is None or h.source == "filename")      # never an AI headline from a failed analysis

    def test_pipeline_skips_failed_ai_without_marking_review_and_retries_later(self):
        cfg = make_cfg(self.tmp / "pipe_err", analysis__transcribe="off")
        bad = FakeVisionProvider(cfg, fail=True)
        rep = run_pipeline(cfg, dry_run=True, rng=random.Random(1), sync_cloud=False, fetch=lambda u: "", provider=bad, max_attempts=2)
        self.assertEqual(rep.status, "no_candidate")
        self.assertTrue(any("AI analysis failed" in r for r in rep.review))
        db = Database(cfg.path("project.data_dir") / "history.db")
        self.assertTrue(all(r["status"] != "review" for r in db.all_videos()))
        self.assertTrue(all(not r["analysis_json"] for r in db.all_videos()))
        db.close()
        good = FakeVisionProvider(cfg)                              # API recovered
        rep = run_pipeline(cfg, dry_run=True, rng=random.Random(1), sync_cloud=False, fetch=lambda u: "", provider=good)
        self.assertEqual(rep.status, "dry_run", rep.message)

    def _http(self, status, body, name="openai"):
        cfg = _cfg(self.tmp / ("http" + str(status)), ai__timeout_seconds=1)
        cls = {"openai": prov_mod.OpenAIProvider, "anthropic": prov_mod.AnthropicProvider,
               "gemini": prov_mod.GeminiProvider}[name]
        resp = mock.Mock(status_code=status, text=json.dumps(body))
        resp.json.return_value = body
        return cls(cfg), resp

    def test_http_401_gives_clear_key_message_and_no_retry(self):
        p, resp = self._http(401, {"error": "bad key"})
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}), \
                mock.patch("reel_agent.ai.base.requests.post", return_value=resp) as post, \
                mock.patch("reel_agent.ai.base.time.sleep"):
            with self.assertRaises(AIError) as cm:
                p.complete_json("s", "p", [])
        self.assertIn("check the key in .env", str(cm.exception))
        self.assertEqual(post.call_count, 1)

    def test_http_429_is_retried_then_succeeds(self):
        body = {"choices": [{"message": {"content": '{"topic": "Unity"}'}}]}
        p, ok = self._http(200, body)
        limited = mock.Mock(status_code=429, text="slow down")
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}), \
                mock.patch("reel_agent.ai.base.requests.post", side_effect=[limited, ok]) as post, \
                mock.patch("reel_agent.ai.base.time.sleep"):
            self.assertEqual(p.complete_json("s", "p", []), {"topic": "Unity"})
        self.assertEqual(post.call_count, 2)

    def test_malformed_json_and_wrong_shape_raise_aierror(self):
        for name, body in (("openai", {"choices": [{"message": {"content": "not json"}}]}),
                           ("anthropic", {"content": [{"type": "text", "text": "no braces"}]}),
                           ("gemini", {"candidates": []})):
            p, ok = self._http(200, body, name)
            with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k", "GEMINI_API_KEY": "k"}), \
                    mock.patch("reel_agent.ai.base.requests.post", return_value=ok):
                with self.assertRaises(AIError, msg=name):
                    p.complete_json("s", "p", [])

    def test_missing_api_key_is_a_clear_error(self):
        from reel_agent.ai.base import get_provider
        cfg = _cfg(self.tmp / "nokey", ai__provider="anthropic")
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(AIError) as cm:
                get_provider(cfg)
        self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))

    def test_requests_carry_images_in_each_providers_format(self):
        cfg = _cfg(self.tmp / "fmt")
        sent = {}
        def fake_post(url, headers=None, json=None, **kw):
            sent[url.split("/")[2]] = (headers, json)
            raise_body = {"choices": [{"message": {"content": "{}"}}], "content": [{"type": "text", "text": "{}"}],
                          "candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
            r = mock.Mock(status_code=200); r.json.return_value = raise_body
            return r
        env = {"OPENAI_API_KEY": "k1", "ANTHROPIC_API_KEY": "k2", "GEMINI_API_KEY": "k3"}
        with mock.patch.dict("os.environ", env), mock.patch("reel_agent.ai.base.requests.post", side_effect=fake_post):
            for cls in (prov_mod.OpenAIProvider, prov_mod.AnthropicProvider, prov_mod.GeminiProvider):
                cls(cfg).complete_json("sys", "prompt", ["QUJD", "REVG"])
        self.assertEqual(sum(c["type"] == "image_url" for c in sent["api.openai.com"][1]["messages"][1]["content"]), 2)
        self.assertEqual(sum(c["type"] == "image" for c in sent["api.anthropic.com"][1]["messages"][0]["content"]), 2)
        self.assertEqual(sum("inline_data" in p for p in
                             sent["generativelanguage.googleapis.com"][1]["contents"][0]["parts"]), 2)
        self.assertEqual(sent["api.openai.com"][0]["Authorization"], "Bearer k1")     # keys come from env only


class CacheTests(Base):
    def test_second_analysis_uses_cache_no_api_call(self):
        cfg = _cfg(self.tmp / "cache")
        p = FakeVisionProvider(cfg)
        v = self.video("unity_game_engine_workflow.mp4")
        a1 = analyze_video(cfg, p, v, video_hash="h" * 64)
        a2 = analyze_video(cfg, p, v, video_hash="h" * 64)
        self.assertEqual(p.n_analysis_calls(), 1)
        self.assertEqual(a1.to_dict(), a2.to_dict())

    def test_cache_invalidated_by_model_or_provider_change_and_refresh(self):
        cfg = _cfg(self.tmp / "cache2")
        p = FakeVisionProvider(cfg)
        v = self.video("unity_game_engine_workflow.mp4")
        analyze_video(cfg, p, v, video_hash="k" * 64)
        cfg.set("ai.models.fake", "other-model")
        analyze_video(cfg, p, v, video_hash="k" * 64)
        analyze_video(cfg, p, v, video_hash="k" * 64, use_cache=False)
        self.assertEqual(p.n_analysis_calls(), 3)
        self.assertNotEqual(AnalysisCache.make_key(cfg, "fake", PROMPT_VERSION), AnalysisCache.make_key(cfg, "x", PROMPT_VERSION))

    def test_cache_can_be_disabled(self):
        cfg = _cfg(self.tmp / "cache3", analysis__cache=False)
        p = FakeVisionProvider(cfg)
        v = self.video("unity_game_engine_workflow.mp4")
        analyze_video(cfg, p, v, video_hash="z" * 64)
        analyze_video(cfg, p, v, video_hash="z" * 64)
        self.assertEqual(p.n_analysis_calls(), 2)

    def test_pipeline_rerun_does_not_repeat_analysis_calls(self):
        cfg = make_cfg(self.tmp / "cache_pipe", analysis__transcribe="off")
        p = FakeVisionProvider(cfg)
        run_pipeline(cfg, dry_run=True, rng=random.Random(2), sync_cloud=False, fetch=lambda u: "", provider=p, force_video=
                     "unity_game_engine_workflow.mp4")
        first = p.n_analysis_calls()
        # fresh DB (as if history was lost) but same data dir cache: still no new analysis for that video
        db_file = cfg.path("project.data_dir") / "history.db"
        db_file.unlink()
        run_pipeline(cfg, dry_run=True, rng=random.Random(2), sync_cloud=False, fetch=lambda u: "", provider=p, force_video=
                     "unity_game_engine_workflow.mp4")
        self.assertEqual(first, 1)
        self.assertEqual(p.n_analysis_calls() - first, 0, "second run re-analysed a cached video")

    def test_heuristic_analysis_never_stands_in_for_ai_analysis(self):
        cfg = make_cfg(self.tmp / "switch", analysis__transcribe="off")
        run_pipeline(cfg, dry_run=True, rng=random.Random(2), sync_cloud=False, fetch=lambda u: "", provider=HeuristicProvider(cfg))
        p = FakeVisionProvider(cfg)
        rep = run_pipeline(cfg, dry_run=True, rng=random.Random(3), sync_cloud=False, fetch=lambda u: "", provider=p)
        self.assertGreaterEqual(p.n_analysis_calls(), 1)
        self.assertTrue(rep.analysis_mode.startswith("ai:"), rep)


class ReviewTests(Base):
    def test_low_confidence_goes_to_review_not_to_a_headline(self):
        cfg = make_cfg(self.tmp / "lowconf", analysis__transcribe="off")
        shutil.copy(self.vdir / "unity_game_engine_workflow.mp4", cfg.path("paths.video_dir") / "x.mp4")
        for f in list(cfg.path("paths.video_dir").glob("*.mp4")):
            if f.name != "x.mp4":
                f.unlink()
        # file name is useless; model is unsure
        p = FakeVisionProvider(cfg, confidence=0.35)
        p._canned = lambda prompt: CANNED["unity_game_engine_workflow.mp4"]
        rep = run_pipeline(cfg, dry_run=True, rng=random.Random(1), sync_cloud=False, fetch=lambda u: "", provider=p)
        self.assertEqual(rep.status, "no_candidate")
        self.assertTrue(any("low confidence" in r for r in rep.review), rep.review)
        db = Database(cfg.path("project.data_dir") / "history.db")
        self.assertEqual([r["status"] for r in db.all_videos()], ["review"])
        self.assertFalse(list(cfg.path("project.output_dir").glob("*/reel.mp4")))
        self.assertFalse([c for c in p.calls if "TASK" in c["prompt"]], "headline must not be generated for review videos")

    def test_model_without_concrete_summary_is_forced_low_confidence(self):
        cfg = _cfg(self.tmp / "vague")
        p = FakeVisionProvider(cfg)
        a = analyze_video(cfg, p, self.video("ai_tools_cursor_agent.mp4"))      # canned reply: "unclear", 0.2
        self.assertLess(a.confidence, 0.6)

    def test_no_valid_headline_sends_video_to_review(self):
        cfg = make_cfg(self.tmp / "nohl", analysis__transcribe="off")
        for f in list(cfg.path("paths.video_dir").glob("*.mp4")):
            if f.name != "unity_game_engine_workflow.mp4":
                f.unlink()
        shutil.move(cfg.path("paths.video_dir") / "unity_game_engine_workflow.mp4", cfg.path("paths.video_dir") / "IMG_2031.mp4")
        p = FakeVisionProvider(cfg, headlines=["Technology in action", "Baking bread"])
        p._canned = lambda prompt: CANNED["unity_game_engine_workflow.mp4"]
        rep = run_pipeline(cfg, dry_run=True, rng=random.Random(1), sync_cloud=False, fetch=lambda u: "", provider=p)
        self.assertEqual(rep.status, "no_candidate")
        db = Database(cfg.path("project.data_dir") / "history.db")
        self.assertEqual([r["status"] for r in db.all_videos()], ["review"])


class HeuristicModeTests(Base):
    def test_heuristic_run_is_clearly_marked_and_uses_descriptive_headline(self):
        cfg = make_cfg(self.tmp / "heur", analysis__transcribe="off")
        rep = run_pipeline(cfg, dry_run=True, rng=random.Random(4), sync_cloud=False, fetch=lambda u: "")
        self.assertEqual(rep.status, "dry_run", rep.message)
        self.assertEqual(rep.analysis_mode, "heuristic")
        self.assertEqual(rep.headline_source, "filename")
        self.assertIn("HEURISTIC MODE", rep.message)
        self.assertNotIn("in action", rep.headline.lower())
        meta = json.loads((Path(rep.output_path).parent / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["analysis_mode"], "heuristic")
        self.assertEqual(meta["headline_source"], "filename")

    def test_ai_run_is_marked_ai_and_publishing_stays_dry(self):
        cfg = make_cfg(self.tmp / "aimode", analysis__transcribe="off")
        p = FakeVisionProvider(cfg)
        rep = run_pipeline(cfg, dry_run=True, rng=random.Random(4), sync_cloud=False, fetch=lambda u: "", provider=p,
                           force_video="unity_game_engine_workflow.mp4")
        self.assertEqual(rep.status, "dry_run", rep.message)
        self.assertEqual(rep.analysis_mode, "ai:fake")
        self.assertEqual(rep.headline_source, "ai")
        self.assertEqual(rep.headline, "Build a water effect in Unity Shader Graph")
        self.assertFalse(cfg.get("publishing.enabled"))
        self.assertTrue(cfg.get("publishing.dry_run"))

    def test_default_settings_never_enable_publishing_or_embed_keys(self):
        from reel_agent.config import PROJECT_ROOT
        text = (PROJECT_ROOT / "settings.yaml").read_text(encoding="utf-8")
        self.assertIn("enabled: false", text)
        import re
        self.assertFalse(re.search(r"sk-[A-Za-z0-9]{10,}|AIza[0-9A-Za-z_-]{20,}", text))
        for f in (PROJECT_ROOT / "reel_agent").rglob("*.py"):
            self.assertFalse(re.search(r"sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}", f.read_text(encoding="utf-8")), f)


if __name__ == "__main__":
    unittest.main()
