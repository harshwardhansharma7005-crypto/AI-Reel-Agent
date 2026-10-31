import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from reel_agent.headline import Headline
from reel_agent.media import ffmpeg_bin, probe
from reel_agent.renderer import RenderError, build_overlay, hex_to_rgb, render_reel
from reel_agent.templates import TEMPLATES, choose_template
from reel_agent.validator import validate_reel
from tests.helpers import make_cfg, sample_library


class RendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg = make_cfg(cls.tmp, copy_media=False)
        vdir, mdir = sample_library()
        cls.video = vdir / "unity_game_engine_workflow.mp4"
        cls.music = next(mdir.glob("*.mp3"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_overlay_layout_for_every_template_respects_margins(self):
        mx = self.cfg.get("reel.safe_margin_x")
        for name, tpl in TEMPLATES.items():
            for text in ("Short headline here", "A much longer headline that needs wrapping across lines"):
                img, lay = build_overlay(self.cfg, Headline(text, ["headline"]), tpl, 1280, 720)
                self.assertEqual(img.size, (1080, 1920))
                x0, y0, x1, y1 = lay.headline_bbox
                self.assertGreaterEqual(x0, mx - 1, name)
                self.assertLessEqual(x1, 1080 - mx + 1, name)
                self.assertGreaterEqual(y0, self.cfg.get("reel.safe_top"))
                vx, vy, vw, vh = lay.video_rect
                self.assertGreater(vy, y1, f"{name}: video overlaps headline")
                self.assertLessEqual(vy + vh, 1920 - self.cfg.get("reel.safe_bottom"))
                self.assertEqual(vx * 2 + vw, 1080, f"{name}: video not horizontally centered")
                self.assertAlmostEqual(vw / vh, 16 / 9, delta=0.02)

    # ---- headline alignment / colour, username text / centering
    def _bright_bbox(self, img, box, pred):
        import numpy as np
        a = np.asarray(img.crop(box)).astype(int)
        ys, xs = np.nonzero(pred(a))
        self.assertTrue(len(xs), "no matching pixels found")
        return xs.min() + box[0], xs.max() + box[0], ys.min() + box[1], ys.max() + box[1]

    def test_headline_lines_centered_for_every_template_and_length(self):
        texts = ("Unity tips", "A smart Unity workflow worth knowing",
                 "Inside the OpenAI AI toolkit that developers are quietly adopting")
        for name, tpl in TEMPLATES.items():
            for text in texts:
                img, lay = build_overlay(self.cfg, Headline(text, ["Unity"]), tpl, 1280, 720)
                x0, _, x1, _ = lay.headline_bbox
                self.assertAlmostEqual((x0 + x1) / 2, 540, delta=3, msg=f"{name}: {text}")
                self.assertGreaterEqual(len(lay.lines), 2, f"{name}: {text}")
                self.assertLessEqual(len(lay.lines), tpl.max_lines)

    def test_headline_balanced_two_lines(self):
        from reel_agent.renderer import load_font, resolve_font_path
        tpl = TEMPLATES["classic"]
        _, lay = build_overlay(self.cfg, Headline("A smart Unity workflow worth knowing", []), tpl, 1280, 720)
        self.assertEqual(len(lay.lines), 2)
        font = load_font(resolve_font_path(self.cfg, tpl), lay.font_size)
        w1, w2 = (font.getlength(l) for l in lay.lines)
        self.assertLess(abs(w1 - w2) / max(w1, w2), 0.45)

    def test_first_line_orange_red_second_line_white(self):
        accent = hex_to_rgb("#FF5733")
        self.assertEqual(accent, (255, 87, 51))
        for name, tpl in TEMPLATES.items():
            img, lay = build_overlay(self.cfg, Headline("A smart Unity workflow worth knowing", ["Unity"]), tpl, 1280, 720)
            x0, y0, x1, y1 = lay.headline_bbox
            lh = (y1 - y0) // len(lay.lines)
            l1 = img.crop((0, y0, 1080, y0 + lh))
            l2 = img.crop((0, y0 + lh, 1080, y1))
            c1, c2 = {c for _, c in l1.getcolors(1 << 20)}, {c for _, c in l2.getcolors(1 << 20)}
            self.assertIn(accent, c1, name)
            self.assertNotIn((255, 255, 255), c1, f"{name}: first line must have no white text")
            self.assertIn((255, 255, 255), c2, name)
            self.assertNotIn(accent, c2, f"{name}: second line must not be orange")

    def test_username_text_and_centering_below_video(self):
        self.assertEqual(self.cfg.get("reel.username"), "@har._shhh._")
        for name, tpl in TEMPLATES.items():
            img, lay = build_overlay(self.cfg, Headline("A smart Unity workflow worth knowing", ["Unity"]), tpl, 1280, 720)
            vx, vy, vw, vh = lay.video_rect
            ux0, uy0, ux1, uy1 = lay.username_bbox
            self.assertAlmostEqual((ux0 + ux1) / 2, 540, delta=2, msg=name)
            self.assertGreaterEqual(uy0, vy + vh, f"{name}: username not below video")
            self.assertLess(uy0 - (vy + vh), 120, f"{name}: username not directly below video")
            # pixel check: light-grey text pixels (all channels high, unlike orange glow) are centred
            lo, hi, _, _ = self._bright_bbox(img, (0, uy0 - 4, 1080, uy1 + 4), lambda a: a.min(axis=2) > 100)
            self.assertAlmostEqual((lo + hi) / 2, 540, delta=6, msg=f"{name}: rendered username off-centre")

    def test_username_can_be_overridden(self):
        cfg = make_cfg(self.tmp / "u", copy_media=False, reel__username="@someone")
        _, lay = build_overlay(cfg, Headline("A smart Unity workflow worth knowing", []), TEMPLATES["classic"], 1280, 720)
        self.assertAlmostEqual((lay.username_bbox[0] + lay.username_bbox[2]) / 2, 540, delta=2)

    def test_headline_too_long_is_rejected_not_clipped(self):
        with self.assertRaises(RenderError):
            build_overlay(self.cfg, Headline("word " * 60, []), TEMPLATES["classic"], 1280, 720)

    def test_render_validates_and_is_instagram_compatible(self):
        out = self.tmp / "out" / "r.mp4"
        hl = Headline("A smart Unity workflow worth knowing", ["Unity"])
        res = render_reel(self.cfg, self.video, self.music, out, hl, TEMPLATES["left_bold"], music_start=1.0)
        info = probe(out)
        self.assertEqual((info["width"], info["height"]), (1080, 1920))
        self.assertEqual((info["vcodec"], info["acodec"], info["pix_fmt"]), ("h264", "aac", "yuv420p"))
        self.assertAlmostEqual(info["duration"], res.duration, delta=0.5)
        rep = validate_reel(self.cfg, out, res.duration, res.layout)
        self.assertTrue(rep.ok, rep.errors)

    def test_original_audio_mix_and_portrait_fit_source(self):
        cfg = make_cfg(self.tmp / "b", copy_media=False, audio__original_audio_volume=0.3, reel__frame_mode="fit_source")
        portrait = self.tmp / "portrait.mp4"
        subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=d=7:s=360x640:r=24",
                        "-f", "lavfi", "-i", "sine=f=440:d=7", "-pix_fmt", "yuv420p", "-shortest", str(portrait)], check=True)
        out = self.tmp / "p.mp4"
        res = render_reel(cfg, portrait, self.music, out, Headline("Portrait source stays unstretched", ["Portrait"]),
                          TEMPLATES["classic"])
        vx, vy, vw, vh = res.layout.video_rect
        self.assertAlmostEqual(vw / vh, 360 / 640, delta=0.02)   # aspect preserved, not stretched
        self.assertTrue(validate_reel(cfg, out, res.duration, res.layout).ok)

    def test_short_music_loops(self):
        short = self.tmp / "short.mp3"
        subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=60:d=3", str(short)], check=True)
        out = self.tmp / "loop.mp4"
        res = render_reel(self.cfg, self.video, short, out, Headline("Looping music test works", ["Looping"]),
                          TEMPLATES["minimal"])
        self.assertTrue(validate_reel(self.cfg, out, res.duration, res.layout).ok)

    def test_too_short_clip_rejected(self):
        with self.assertRaises(RenderError):
            render_reel(self.cfg, self.video, self.music, self.tmp / "x.mp4", Headline("Clip start too late", []),
                        TEMPLATES["classic"], clip_start=5.0)

    def test_validator_catches_bad_files(self):
        bad = self.tmp / "bad.mp4"      # 16:9 landscape, not 1080x1920
        shutil.copy(self.video, bad)
        rep = validate_reel(self.cfg, bad)
        self.assertFalse(rep.ok)
        self.assertIn("dimensions", " ".join(rep.errors))
        self.assertFalse(validate_reel(self.cfg, self.tmp / "missing.mp4").ok)

    def test_validator_catches_silent_audio(self):
        out = self.tmp / "ok.mp4"
        res = render_reel(self.cfg, self.video, self.music, out, Headline("Silent audio gets caught", ["Silent"]),
                          TEMPLATES["classic"])
        silent = self.tmp / "silent.mp4"
        subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(out), "-af", "volume=0", "-c:v", "copy",
                        "-c:a", "aac", str(silent)], check=True)
        rep = validate_reel(self.cfg, silent, res.duration, res.layout)
        self.assertFalse(rep.ok)
        self.assertIn("audio_not_silent", " ".join(rep.errors))

    def test_template_variation_is_controlled(self):
        recent = []
        import random
        rng = random.Random(3)
        for _ in range(12):
            t = choose_template(self.cfg, recent, rng)
            if recent:
                self.assertNotEqual(t.name, recent[0])
            recent.insert(0, t.name)
        self.assertGreaterEqual(len(set(recent)), 3)


if __name__ == "__main__":
    unittest.main()
