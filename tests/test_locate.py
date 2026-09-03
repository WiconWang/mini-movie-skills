import unittest

from mmm.locate import normalize_text, locate_quote, snap_interval


def _words(items):
    return [{"text": t, "start": s, "end": e} for t, s, e in items]


class LocateTests(unittest.TestCase):
    def test_normalize_removes_punctuation(self):
        self.assertEqual(normalize_text("可以吗? 蒙德!"), "可以吗蒙德")

    def test_exact_match_returns_span(self):
        words = _words([
            ("啊。", 1241.4, 1241.86),
            ("可以", 1242.58, 1242.88),
            ("吗?", 1242.88, 1243.3),
            ("可以", 1243.54, 1243.78),
            ("吗?", 1243.78, 1243.98),
            ("可以", 1244.16, 1244.24),
            ("吗?", 1244.24, 1244.54),
        ])
        r = locate_quote(words, "可以吗 可以吗 可以吗", pad=0.0)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["start"], 1242.58, places=3)
        self.assertAlmostEqual(r["end"], 1244.54, places=3)

    def test_exact_scored_one(self):
        words = _words([("可以", 1.0, 1.4), ("吗", 1.4, 1.8)])
        r = locate_quote(words, "可以吗", pad=0.0)
        self.assertEqual(r["score"], 1.0)

    def test_fuzzy_match_tolerates_diff(self):
        words = _words([("蒙德", 1.0, 1.4), ("城", 1.4, 1.8), ("到了", 1.8, 2.3)])
        r = locate_quote(words, "蒙德城到了", threshold=0.6, pad=0.0)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["start"], 1.0, places=3)
        self.assertAlmostEqual(r["end"], 2.3, places=3)

    def test_pad_clamps_start(self):
        words = _words([("可以", 0.3, 0.7), ("吗", 0.7, 1.0)])
        r = locate_quote(words, "可以吗", pad=0.5)
        self.assertAlmostEqual(r["start"], 0.0, places=3)
        self.assertGreaterEqual(r["end"], 1.0)

    def test_no_match_returns_none(self):
        words = _words([("苹果", 1.0, 1.5)])
        self.assertIsNone(locate_quote(words, "完全不同的句子", threshold=0.6, pad=0.0))


    def test_snap_overlap_adsorbs_to_speech(self):
        words = _words([("前奏", 100.0, 101.0), ("可以", 102.5, 102.9),
                        ("吗", 102.9, 103.3), ("哦", 105.0, 105.4)])
        r = snap_interval(words, 102.0, 104.0)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["start"], 102.5, places=3)
        self.assertAlmostEqual(r["end"], 103.3, places=3)
        self.assertEqual(r["matched_text"], "可以吗")

    def test_snap_slop_grabs_nearest_speech(self):
        words = _words([("可以", 100.0, 100.4), ("吗", 100.4, 100.8), ("哦", 100.8, 101.2)])
        r = snap_interval(words, 99.0, 99.3, slop=1.0)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["start"], 100.0, places=3)

    def test_snap_no_speech_returns_none(self):
        words = _words([("可以是", 300.0, 300.5)])
        self.assertIsNone(snap_interval(words, 100.0, 101.0, slop=1.0))



if __name__ == "__main__":
    unittest.main()
