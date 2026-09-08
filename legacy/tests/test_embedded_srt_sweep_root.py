"""Regression tests for run_embedded_srt_sweep's library-root selection.

The sweep must prefer the real NAS mount /mnt/nas/share/media/jellyfin
when that path exists, and otherwise fall back to the configured
JELLYFIN_MEDIA_ROOT (then the module default). Previously the sweep used
JELLYFIN_MEDIA_ROOT unconditionally, which in the container is the
Jellyfin-internal /media view and can be empty - so the sweep would scan
nothing. We assert which root os.walk is called on without running any
ffmpeg: with an empty walk the loop body never executes, keeping the test
hermetic.
"""

import os
import unittest
from unittest.mock import patch

from tests import HermeticStateMixin

import orchestrator as o


# The corrected library root the sweep prefers when it exists.
NAS_LIBRARY_ROOT = "/mnt/nas/share/media/jellyfin"


class TestEmbeddedSrtSweepRoot(HermeticStateMixin):
    def _run(self, cfg, isdir_f):
        """Run the sweep with a controlled isdir predicate and an os.walk
        that records the root it is asked to traverse. Returns
        (result, roots_walked)."""
        walked = []

        def fake_walk(root, *_a, **_k):
            walked.append(root)
            # Empty walk: never scan a file, so no ffmpeg/ffprobe runs.
            return iter(())

        with patch.object(o.os.path, "isdir", side_effect=isdir_f), patch.object(
            o.os, "walk", side_effect=fake_walk
        ):
            res = o.run_embedded_srt_sweep(cfg, budget=5)
        return res, walked

    def test_prefers_nas_mount_when_it_exists(self):
        cfg = {"JELLYFIN_MEDIA_ROOT": "/fallback/root"}
        # NAS mount exists -> isdir(/mnt/nas/...) True, fallback False.

        def isdir(path):
            return path == NAS_LIBRARY_ROOT

        res, walked = self._run(cfg, isdir)
        self.assertEqual(walked, [NAS_LIBRARY_ROOT])
        self.assertEqual(res["extracted"], 0)
        self.assertEqual(res["failed"], [])

    def test_falls_back_to_configured_root_when_mount_missing(self):
        cfg = {"JELLYFIN_MEDIA_ROOT": "/fallback/root"}
        # Mount missing but explicit JELLYFIN_MEDIA_ROOT configured.
        res, walked = self._run(cfg, lambda path: False)
        self.assertEqual(walked, ["/fallback/root"])

    def test_cfg_root_wins_over_module_default_without_mount(self):
        # Mount missing but explicit JELLYFIN_MEDIA_ROOT configured.
        cfg = {"JELLYFIN_MEDIA_ROOT": "/custom/root"}
        res, walked = self._run(cfg, lambda path: False)
        self.assertEqual(walked, ["/custom/root"])
        # It must NOT have silently fallen back to the module default /media.
        self.assertNotIn("/media", walked)

    def test_falls_back_to_module_default_when_no_cfg_and_no_mount(self):
        # No JELLYFIN_MEDIA_ROOT in cfg and mount missing -> module default.
        res, walked = self._run({}, lambda path: False)
        self.assertEqual(walked, [o.JELLYFIN_MEDIA_ROOT])


if __name__ == "__main__":
    unittest.main()
