import os
import tempfile
import unittest

from tests import HermeticStateMixin

import orchestrator as o


def _srt(cues):
    """cues: list of (start_s, end_s) -> minimal SRT text."""
    def ts(s):
        ms = int(round(s * 1000))
        return "%02d:%02d:%02d,%03d" % (
            ms // 3600000, ms % 3600000 // 60000, ms % 60000 // 1000, ms % 1000
        )
    out = []
    for i, (a, b) in enumerate(cues, 1):
        out.append(f"{i}\n{ts(a)} --> {ts(b)}\nテスト字幕\n")
    return "\n".join(out) + "\n"


class TestTimelineGate(HermeticStateMixin):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()

    def _file(self, cues):
        p = os.path.join(self.tmp, "t.srt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(_srt(cues))
        return p

    def test_healthy_timeline_passes(self):
        cues = [(i * 4.0, i * 4.0 + 3.0) for i in range(1, 60)]
        ok, why = o.validate_srt_timeline(self._file(cues), 240.0)
        self.assertTrue(ok, why)
        self.assertEqual(why, "ok")

    def test_pileup_rejected(self):
        # DP S01E09 signature: 9 consecutive cues share one start after a hole
        cues = [(i * 4.0, i * 4.0 + 3.0) for i in range(1, 100)]
        cues += [(986.1, 988.0)] * 9
        cues += [(1200.0, 1203.0), (1250.0, 1253.0)]
        ok, why = o.validate_srt_timeline(self._file(cues), 1469.7)
        self.assertFalse(ok)
        self.assertIn("pileup", why)

    def test_two_cues_sharing_start_allowed(self):
        # dialogue split across two simultaneous speaker cues is normal
        cues = [(i * 5.0, i * 5.0 + 2.0) for i in range(1, 80)]
        cues.insert(30, (150.0, 152.0))  # second cue at same start
        ok, why = o.validate_srt_timeline(self._file(cues), 400.0)
        self.assertTrue(ok, why)

    def test_internal_hole_rejected(self):
        # 300s+ mid-file hole on a 1400s video (> 30% of runtime? no: 21% —
        # but coverage still fails because the span check needs the END to
        # reach; use a hole > MAX_GAP_FRAC instead: 500s of 1400s = 35%)
        cues = [(i * 4.0, i * 4.0 + 3.0) for i in range(1, 50)]          # 4..196
        cues += [(700.0 + i * 4.0, 703.0 + i * 4.0) for i in range(50)]  # gap 196->700 = 504s
        ok, why = o.validate_srt_timeline(self._file(cues), 900.0)
        self.assertFalse(ok)
        self.assertIn("gap", why)

    def test_coverage_shrink_rejected(self):
        # sub ends at 40% of the video: collapsed retime signature
        cues = [(i * 4.0, i * 4.0 + 3.0) for i in range(1, 100)]
        ok, why = o.validate_srt_timeline(self._file(cues), 1000.0)
        self.assertFalse(ok)
        self.assertIn("coverage", why)

    def test_monotonicity_enforced(self):
        cues = [(10.0, 12.0), (5.0, 6.0), (20.0, 22.0)]
        ok, why = o.validate_srt_timeline(self._file(cues), None)
        self.assertFalse(ok)
        self.assertIn("monotonic", why)

    def test_no_duration_skips_span_checks(self):
        cues = [(i * 4.0, i * 4.0 + 3.0) for i in range(1, 60)]
        ok, why = o.validate_srt_timeline(self._file(cues), None)
        self.assertTrue(ok, why)

    def test_real_collapsed_file_rejected(self):
        # the actual DP S01E09 backup from production (if present on host)
        p = "/tmp/e09_ja_collapsed.bak.srt"
        if not os.path.isfile(p):
            self.skipTest("production backup not on this host")
        ok, why = o.validate_srt_timeline(p, None)
        self.assertFalse(ok)
        self.assertIn("pileup", why)


if __name__ == "__main__":
    unittest.main()
