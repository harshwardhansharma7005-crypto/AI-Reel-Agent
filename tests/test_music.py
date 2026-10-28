import random
import shutil
import tempfile
import unittest
from pathlib import Path

from reel_agent.db import Database
from reel_agent.music import Track, choose_start, choose_track, scan_music
from tests.helpers import make_cfg


class MusicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = make_cfg(self.tmp)
        self.db = Database(self.tmp / "h.db")
        self.addCleanup(self.db.close)

    def test_scan_finds_tracks(self):
        tracks = scan_music(self.cfg)
        self.assertEqual(len(tracks), 3)
        self.assertTrue(all(t.duration > 25 for t in tracks))

    def test_ignores_non_audio_and_too_short(self):
        (self.tmp / "music" / "notes.txt").write_text("hi")
        tracks = scan_music(self.cfg, self.tmp / "music")
        self.assertEqual(len(tracks), 3)

    def test_never_same_track_twice_in_a_row(self):
        tracks = scan_music(self.cfg)
        rng = random.Random(1)
        prev = None
        for _ in range(40):
            t = choose_track(self.cfg, self.db, tracks, rng)
            self.assertNotEqual(t.path, prev)
            self.db.mark_song_used(t.path)
            prev = t.path

    def test_rotation_uses_every_track(self):
        tracks = scan_music(self.cfg)
        rng = random.Random(7)
        used = []
        for _ in range(6):
            t = choose_track(self.cfg, self.db, tracks, rng)
            self.db.mark_song_used(t.path)
            used.append(t.path)
        self.assertEqual(set(used), {t.path for t in tracks})

    def test_single_track_library_still_works(self):
        one = scan_music(self.cfg)[:1]
        self.db.mark_song_used(one[0].path)
        self.assertEqual(choose_track(self.cfg, self.db, one).path, one[0].path)

    def test_empty_library_raises(self):
        with self.assertRaises(ValueError):
            choose_track(self.cfg, self.db, [])

    def test_start_offset_leaves_room(self):
        t = Track("x.mp3", 100.0)
        for seed in range(20):
            s = choose_start(t, 45, random.Random(seed))
            self.assertTrue(0 <= s <= 100 - 45)
        self.assertEqual(choose_start(Track("y.mp3", 20.0), 45), 0.0)


if __name__ == "__main__":
    unittest.main()
