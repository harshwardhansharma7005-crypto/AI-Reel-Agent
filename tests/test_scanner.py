import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from reel_agent.db import Database
from reel_agent.media import ffmpeg_bin
from reel_agent.scanner import scan_videos
from tests.helpers import make_cfg


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = make_cfg(self.tmp)
        self.db = Database(self.tmp / "h.db")
        self.addCleanup(self.db.close)
        self.vdir = self.tmp / "videos"

    def test_scans_all_distinct_videos(self):
        r = scan_videos(self.cfg, self.db)
        self.assertEqual(len(r.new), 6)
        self.assertEqual(r.duplicates, [])
        self.assertEqual(len(self.db.all_videos()), 6)

    def test_rescan_is_idempotent(self):
        scan_videos(self.cfg, self.db)
        r = scan_videos(self.cfg, self.db)
        self.assertEqual((len(r.new), len(r.known)), (0, 6))

    def test_exact_copy_with_new_name_is_duplicate(self):
        shutil.copy(self.vdir / "unity_game_engine_workflow.mp4", self.vdir / "zz_copy.mp4")
        r = scan_videos(self.cfg, self.db)
        self.assertEqual(len(r.duplicates), 1)
        self.assertEqual(len(r.new), 6)

    def test_reencoded_video_is_near_duplicate(self):
        src = self.vdir / "android_kotlin_compose_tips.mp4"
        out = self.vdir / "zz_reencoded.mp4"
        subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(src), "-vf", "scale=960:540", "-crf", "35",
                        "-c:a", "aac", "-b:a", "64k", str(out)], check=True)
        r = scan_videos(self.cfg, self.db)
        dup_names = [Path(p).name for p, _ in r.duplicates]
        self.assertIn("zz_reencoded.mp4", dup_names)
        self.assertEqual(len(r.new), 6)

    def test_too_short_and_corrupt_files_skipped(self):
        short = self.vdir / "short.mp4"
        subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=2:s=320x240",
                        "-pix_fmt", "yuv420p", str(short)], check=True)
        (self.vdir / "corrupt.mp4").write_bytes(b"not a video" * 100)
        r = scan_videos(self.cfg, self.db)
        reasons = {Path(p).name: why for p, why in r.skipped}
        self.assertIn("short.mp4", reasons)
        self.assertIn("corrupt.mp4", reasons)

    def test_missing_folder_is_empty_not_crash(self):
        r = scan_videos(self.cfg, self.db, folder=self.tmp / "does_not_exist")
        self.assertEqual(r.total_usable, 0)


if __name__ == "__main__":
    unittest.main()
