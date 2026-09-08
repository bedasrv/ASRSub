import json
import os
import tempfile
import unittest

import control_api_v2 as api2
import orchestrator as o


class TestAsrSourceMarkerOwnership(unittest.TestCase):
    def _case(self, marker):
        tmp = tempfile.TemporaryDirectory(prefix="asrsub-asr-source-marker-")
        stem = os.path.join(tmp.name, "Show")
        media = stem + ".mkv"
        source = stem + ".id.srt"
        registry = os.path.join(tmp.name, "registry.jsonl")
        open(media, "wb").close()
        text = "1\n00:00:00,000 --> 00:00:01,000\n"
        text += o.AI_MARKER + "\nOwned.\n" if marker else "Foreign.\n"
        with open(source, "w", encoding="utf-8") as fh:
            fh.write(text)
        row = {
            "stem": stem, "lang": "id", "source": "asr",
            "source_path": source, "source_hash": o.file_sha256(source),
        }
        return tmp, stem, media, source, registry, row

    def test_foreign_asr_source_file_is_not_current_or_reconciled(self):
        tmp, stem, media, source, registry, row = self._case(False)
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(o._registry_current_row(stem, "id", media_path=media, row=row))
        self.assertFalse(o.sub_is_ai_owned({(stem, "id"): row}, 7, "id", media))
        self.assertFalse(api2.ControlAPIv2({})._registry_row_current(row, media, "id"))
        with open(registry, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        result = o.reconcile_registry(registry, budget=1)
        self.assertEqual(result["reconciled"], 0, result)
        with open(registry, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), json.dumps(row) + "\n")

    def test_marker_bearing_asr_source_file_can_be_current_and_reconciled(self):
        tmp, stem, media, source, registry, row = self._case(True)
        self.addCleanup(tmp.cleanup)
        self.assertIsNotNone(o._registry_current_row(stem, "id", media_path=media, row=row))
        self.assertTrue(o.sub_is_ai_owned({(stem, "id"): row}, 7, "id", media))
        self.assertTrue(api2.ControlAPIv2({})._registry_row_current(row, media, "id"))
        with open(registry, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        result = o.reconcile_registry(registry, budget=1)
        self.assertIn(result["reconciled"], (0, 1), result)
        self.assertEqual(result["invalid"], 0, result)


if __name__ == "__main__":
    unittest.main()
