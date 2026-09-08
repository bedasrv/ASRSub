import json
import os
import tempfile
import unittest
from unittest.mock import patch

import orchestrator as o


class TestRegistryLockAndTmpLadder(unittest.TestCase):
    def test_shared_registry_read_creates_no_parent_or_lock(self):
        with tempfile.TemporaryDirectory(prefix="asrsub-read-lock-") as tmp:
            registry = os.path.join(tmp, "missing", "registry.jsonl")
            with patch.object(o, "REGISTRY_FILE", registry):
                self.assertIsNone(o.registry_get("/tmp/Show", "id"))
            self.assertFalse(os.path.exists(os.path.dirname(registry)))
            self.assertFalse(os.path.exists(registry + ".lock"))

    def test_nested_shared_lock_upgrades_for_exclusive_write(self):
        with tempfile.TemporaryDirectory(prefix="asrsub-nested-lock-") as tmp:
            registry = os.path.join(tmp, "registry.jsonl")
            modes = []
            real_flock = o.fcntl.flock

            def capture(fd, mode):
                modes.append(mode)
                return real_flock(fd, mode)

            with patch.object(o, "REGISTRY_FILE", registry), patch.object(o.fcntl, "flock", side_effect=capture):
                with o._registry_lock(registry):
                    with o._registry_lock(registry, exclusive=True):
                        o.registry_upsert("/tmp/Show", "id", "asr")
            self.assertIn(o.fcntl.LOCK_SH, modes)
            self.assertIn(o.fcntl.LOCK_EX, modes)

    def test_tmp_embedded_ladder_row_stays_current_after_tmp_deleted(self):
        with tempfile.TemporaryDirectory(prefix="asrsub-tmp-ladder-") as tmp:
            media = os.path.join(tmp, "Show.mkv")
            source_path = os.path.join(tmp, "extracted.srt")
            registry = os.path.join(tmp, "registry.jsonl")
            open(media, "wb").close()
            with open(source_path, "w", encoding="utf-8") as fh:
                fh.write("embedded temporary source")
            source = {
                "kind": "jpn", "tmp": True, "source_path": source_path,
                "source_hash": o.file_sha256(source_path),
                "cues": [{"start": 0, "end": 1000, "text": "日本語"}],
            }
            info = {"episodeFile": {"path": media}, "seriesId": 1, "title": "Show"}
            with patch.object(o, "REGISTRY_FILE", registry), patch.object(
                o, "translate_texts", return_value=[(0, 1, "Translated")]
            ), patch.object(o, "upload_srt", return_value=204), patch.object(
                o, "jellyfin_refresh"), patch.object(o, "append_state"), patch.object(
                o, "notify_webhook"), patch.object(o, "log"), patch.object(
                o, "notify_hermes"), patch.object(o, "halt_on_error"):
                self.assertEqual(o.process_ladder({"TMP_DIR": tmp}, "key", 1, "id", "Show", "S01E01", source, info), "done")
                os.unlink(source_path) if os.path.exists(source_path) else None
                stem = os.path.splitext(media)[0]
                row = o.registry_get(stem, "id")
                self.assertTrue(row)
                self.assertEqual(row["source_kind"], "embedded")
                self.assertEqual(row["source_path"], "")
                self.assertEqual(row["source_hash"], "")
                self.assertTrue(o._registry_current_row(stem, "id", media_path=media, row=row))
                self.assertNotIn(source_path, json.dumps(row))


if __name__ == "__main__":
    unittest.main()
