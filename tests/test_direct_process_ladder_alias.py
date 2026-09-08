import json
import os
import tempfile
import unittest
from unittest.mock import patch

import orchestrator as o


class TestDirectProcessLadderAlias(unittest.TestCase):
    def test_jpn_alias_uses_canonical_target_everywhere(self):
        with tempfile.TemporaryDirectory(prefix="asrsub-direct-ladder-alias-") as tmp:
            media = os.path.join(tmp, "Show.mkv")
            source_path = os.path.join(tmp, "Show.jpn.srt")
            registry = os.path.join(tmp, "registry.jsonl")
            open(media, "wb").close()
            with open(source_path, "w", encoding="utf-8") as fh:
                fh.write("1\n00:00:00,000 --> 00:00:01,000\n日本語の台詞です。\n")
            source = {
                "kind": "jpn", "source_path": source_path,
                "source_hash": o.file_sha256(source_path),
                "cues": [{"start": 0, "end": 1000, "text": "日本語の台詞です。"}],
                "tmp": False,
            }
            info = {"episodeFile": {"path": media}, "seriesId": 4, "title": "Show"}
            uploaded = []
            states = []
            with patch.object(o, "REGISTRY_FILE", registry), patch.object(
                o, "translate_texts", return_value=[(0, 1, "Translated.")]
            ), patch.object(
                o, "upload_srt", side_effect=lambda *a, **k: uploaded.append((a, k)) or 204
            ), patch.object(o, "jellyfin_refresh"), patch.object(
                o, "notify_webhook"
            ), patch.object(o, "append_state", side_effect=states.append), patch.object(
                o, "log"
            ), patch.object(o, "notify_hermes"), patch.object(o, "halt_on_error"):
                result = o.process_ladder(
                    {"TMP_DIR": tmp}, "key", 12, "JPN", "Show", "S01E01", source, info
                )
            self.assertEqual(result, "done")
            self.assertEqual(uploaded[0][0][3], "ja")
            self.assertEqual(states[0]["language"], "ja")
            self.assertTrue(os.path.isfile(os.path.join(tmp, "Show.ja.hi.srt")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "Show.JPN.srt")))
            with open(registry, encoding="utf-8") as fh:
                row = json.loads(fh.readline())
            self.assertEqual(row["lang"], "ja")


if __name__ == "__main__":
    unittest.main()
