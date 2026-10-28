import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reel_agent.config import ConfigError, deep_merge, load_config, validate_config, PROJECT_ROOT


class ConfigTests(unittest.TestCase):
    def test_default_settings_are_valid(self):
        cfg = load_config(PROJECT_ROOT / "settings.yaml", load_env=False)
        self.assertEqual(validate_config(cfg), [])
        self.assertEqual((cfg.get("reel.width"), cfg.get("reel.height")), (1080, 1920))
        self.assertEqual(cfg.get("headline.highlight_color"), "#FF5733")

    def test_publishing_is_off_by_default(self):
        cfg = load_config(PROJECT_ROOT / "settings.yaml", load_env=False)
        self.assertFalse(cfg.get("publishing.enabled"))
        self.assertTrue(cfg.get("publishing.dry_run"))
        self.assertFalse(cfg.get("music.i_have_rights_to_publish_all_tracks"))

    def test_validation_catches_problems(self):
        cfg = load_config(PROJECT_ROOT / "settings.yaml", load_env=False, overrides={
            "reel": {"width": 720}, "schedule": {"slots": {"morning": "25:99"}},
            "ai": {"provider": "nope"}, "headline": {"highlight_color": "orange"}})
        errs = " | ".join(validate_config(cfg))
        for needle in ("1080x1920", "HH:MM", "ai.provider", "highlight_color"):
            self.assertIn(needle, errs)

    def test_deep_merge_keeps_other_keys(self):
        out = deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}})
        self.assertEqual(out, {"a": {"b": 9, "c": 2}})

    def test_missing_and_invalid_files(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ConfigError):
                load_config(Path(d) / "nope.yaml", load_env=False)
            bad = Path(d) / "bad.yaml"
            bad.write_text("a: [unclosed", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(bad, load_env=False)

    def test_env_overrides(self):
        with mock.patch.dict(os.environ, {"REEL_USERNAME": "@brand", "REEL_VIDEO_DIR": "/x/vids"}):
            cfg = load_config(PROJECT_ROOT / "settings.yaml", load_env=False)
        self.assertEqual(cfg.get("reel.username"), "@brand")
        self.assertEqual(cfg.get("paths.video_dir"), "/x/vids")

    def test_workflow_cron_matches_ist_slots(self):
        """10:00 IST = 04:30 UTC, 20:00 IST = 14:30 UTC (IST is UTC+5:30, no DST)."""
        import yaml
        wf = yaml.safe_load((PROJECT_ROOT / ".github/workflows/reels.yml").read_text(encoding="utf-8"))
        triggers = wf.get("on") or wf.get(True)
        crons = sorted(c["cron"] for c in triggers["schedule"])
        self.assertEqual(crons, ["30 14 * * *", "30 4 * * *"])
        self.assertIn("workflow_dispatch", triggers)
        # arithmetic check
        for cron, ist_h in (("30 4 * * *", 10), ("30 14 * * *", 20)):
            m, h = map(int, cron.split()[:2])
            total = (h * 60 + m + 330) % 1440
            self.assertEqual((total // 60, total % 60), (ist_h, 0))

    def test_workflow_manual_dry_run_cannot_publish_via_variable(self):
        """Regression: `cond && inputs.publish || var` would publish when inputs.publish is false."""
        text = (PROJECT_ROOT / ".github/workflows/reels.yml").read_text(encoding="utf-8")
        self.assertNotIn("&& inputs.publish ||", text)
        self.assertIn('if [ "$EVENT_NAME" = "workflow_dispatch" ]', text)
        self.assertIn('DO_PUBLISH="$INPUT_PUBLISH"', text)


if __name__ == "__main__":
    unittest.main()
