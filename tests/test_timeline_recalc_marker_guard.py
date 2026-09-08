import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests import HermeticStateMixin

import orchestrator as o


class TestTimelineRecalcMarkerGuard(HermeticStateMixin):
    def test_asr_row_without_marker_is_not_repaired(self):
        tmp = tempfile.mkdtemp(prefix="asrsub-marker-guard-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        media = os.path.join(tmp, "Show.mkv")
        sidecar = os.path.join(tmp, "Show.ja.srt")
        registry = os.path.join(tmp, "registry.jsonl")
        open(media, "wb").close()
        with open(sidecar, "wb") as fh:
            fh.write(b"1\n00:00:00,000 --> 00:00:01,000\nforeign text\n")
        digest = o.file_sha256(sidecar)
        row = {
            "stem": os.path.splitext(media)[0],
            "lang": "ja",
            "source": "asr",
            "source_path": sidecar,
            "source_hash": digest,
            "episode_id": 1,
        }
        with open(registry, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        before = open(sidecar, "rb").read()
        cache = [{"start": 0, "end": 1000, "text": "cache rebuild"}]
        with patch.object(o, "REGISTRY_FILE", registry), patch.object(
            o, "validate_srt_timeline", side_effect=[(False, "broken"), (True, "ok")]
        ), patch.object(o, "media_duration_s", return_value=60.0), patch.object(
            o, "probe_audio", return_value=[]
        ), patch.object(o, "audio_stream_signature", return_value="audio"), patch.object(
            o, "asr_cache_get", return_value=cache
        ):
            result = o.run_timeline_recalc({"TIMELINE_RECALC_BUDGET": 1})
        self.assertEqual(result["repaired"], 0)
        self.assertEqual(open(sidecar, "rb").read(), before)
        with open(registry, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(rows, [row])


if __name__ == "__main__":
    unittest.main()
