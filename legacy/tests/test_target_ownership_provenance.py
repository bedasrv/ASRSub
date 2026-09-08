import os
import tempfile
import unittest

import control_api_v2 as api2
import orchestrator as o


class TestTargetOwnershipProvenance(unittest.TestCase):
    def _fixture(self, marker=False):
        tmp = tempfile.TemporaryDirectory(prefix="asrsub-target-owner-")
        stem = os.path.join(tmp.name, "Show")
        media = stem + ".mkv"
        source = stem + ".jpn.srt"
        target = stem + ".id.srt"
        open(media, "wb").close()
        with open(source, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\nJapanese source.\n")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\n"
                      + (o.AI_MARKER + "\nOwned target.\n" if marker else "Foreign target.\n"))
        row = {
            "stem": stem, "lang": "id", "source": "jpn", "episode_id": 7,
            "source_path": source, "source_hash": o.file_sha256(source),
        }
        if marker:
            row["target_path"] = target
            row["target_hash"] = o.file_sha256(target)
        return tmp, media, target, row

    def test_foreign_target_cannot_claim_legacy_translated_row(self):
        tmp, media, _target, row = self._fixture()
        self.addCleanup(tmp.cleanup)
        stem = os.path.splitext(media)[0]
        self.assertIsNone(o._registry_current_row(stem, "id", media_path=media, row=row))
        self.assertFalse(o.sub_is_ai_owned({(stem, "id"): row}, 7, "id", media))
        api = api2.ControlAPIv2({})
        self.assertFalse(api._registry_row_current(row, media, "id"))

    def test_marker_target_with_consistent_hash_can_be_current(self):
        tmp, media, _target, row = self._fixture(marker=True)
        self.addCleanup(tmp.cleanup)
        stem = os.path.splitext(media)[0]
        self.assertIsNotNone(o._registry_current_row(stem, "id", media_path=media, row=row))
        api = api2.ControlAPIv2({})
        self.assertTrue(api._registry_row_current(row, media, "id"))


if __name__ == "__main__":
    unittest.main()
