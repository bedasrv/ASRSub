import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests import HermeticStateMixin

import orchestrator as o


class TestAdoptionProvenance(HermeticStateMixin):
    def test_stale_row_adoption_preserves_episode_and_kind(self):
        tmp = tempfile.mkdtemp(prefix="asrsub-adoption-provenance-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        media = os.path.join(tmp, "Movie.mkv")
        sidecar = os.path.join(tmp, "Movie.ja.srt")
        registry = os.path.join(tmp, "registry.jsonl")
        open(media, "wb").close()
        body = b"1\n00:00:00,000 --> 00:00:01,000\nembedded\n"
        with open(sidecar, "wb") as fh:
            fh.write(body)
        stale = {
            "stem": os.path.splitext(media)[0],
            "lang": "jpn",
            "source": "jpn",
            "source_kind": "external",
            "source_path": os.path.join(tmp, "gone.srt"),
            "source_hash": "wrong",
            "episode_id": 77,
            "kind": "movie",
        }
        with open(registry, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(stale) + "\n")

        def extract(_media, _lang, out):
            with open(out, "wb") as fh:
                fh.write(body)
            return True

        with patch.object(o, "REGISTRY_FILE", registry), patch.object(
            o, "extract_embedded_subtitle", side_effect=extract
        ), patch.object(o, "probe_audio", return_value=[]), patch.object(
            o, "audio_stream_signature", return_value="audio"
        ):
            self.assertTrue(o._adopt_embedded(os.path.splitext(media)[0], "jpn", media, sidecar, tmp))

        rows = [json.loads(line) for line in open(registry, encoding="utf-8")]
        current = rows[-1]
        self.assertEqual(current["episode_id"], 77)
        self.assertEqual(current["kind"], "movie")
        self.assertEqual(current["source"], "embedded")
        self.assertEqual(current["source_kind"], "embedded")
        self.assertEqual(current["lang"], "ja")


if __name__ == "__main__":
    unittest.main()
