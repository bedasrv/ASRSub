import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests import HermeticStateMixin

import orchestrator as o


class TestLanguageRegistryAdversarial6(HermeticStateMixin):
    def test_stale_registry_row_does_not_block_verified_adoption(self):
        tmp = tempfile.mkdtemp(prefix="asrsub-adversarial6-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        media = os.path.join(tmp, "Show.mkv")
        sidecar = os.path.join(tmp, "Show.ja.srt")
        registry = os.path.join(tmp, "registry.jsonl")
        open(media, "wb").close()
        body = b"1\n00:00:00,000 --> 00:00:01,000\nverified embedded\n"
        with open(sidecar, "wb") as fh:
            fh.write(body)
        stale = {
            "stem": os.path.splitext(media)[0],
            "lang": "ja",
            "source": "jpn",
            "source_kind": "external",
            "source_path": os.path.join(tmp, "gone.srt"),
            "source_hash": "wrong",
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
            adopted = o._adopt_embedded(
                os.path.splitext(media)[0], "jpn", media, sidecar, tmp
            )

        self.assertTrue(adopted)
        rows = [json.loads(line) for line in open(registry, encoding="utf-8")]
        self.assertEqual(rows[-1]["lang"], "ja")
        self.assertEqual(rows[-1]["source_kind"], "embedded")
        self.assertEqual(rows[-1]["source_path"], sidecar)
        self.assertEqual(rows[-1]["source_hash"], o.file_sha256(sidecar))


if __name__ == "__main__":
    unittest.main()
