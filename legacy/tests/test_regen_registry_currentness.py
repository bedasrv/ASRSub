import os
import tempfile
import unittest
from unittest.mock import patch

import orchestrator as o
from pipeline import dry_tests


class TestRegenRegistryCurrentness(unittest.TestCase):
    def _capture_regen_items(self, tmp, registry_rows):
        media = os.path.join(tmp, "Episode.mkv")
        open(media, "wb").close()
        captured = []

        def capture(_cfg, _wanted, _movies, regen_items, _max_eps):
            captured.extend(regen_items)
            return [], 0

        with patch.object(o, "_order_pass_candidates", side_effect=capture):
            dry_tests._run_pass_regen(
                tmp, {7: {"hasFile": True, "seasonNumber": 1, "episodeNumber": 1,
                        "episodeFile": {"path": media}}},
                state=[{"sonarrEpisodeId": 7, "language": "id", "status": "done",
                        "seriesTitle": "Episode"}], regen=True,
                target_langs=["id"], registry_rows=registry_rows,
            )
        return captured

    def test_stale_pathless_asr_row_does_not_suppress_regen(self):
        with tempfile.TemporaryDirectory(prefix="asrsub-regen-stale-") as tmp:
            stem = os.path.join(tmp, "Episode")
            items = self._capture_regen_items(tmp, [{
                "stem": stem, "lang": "id", "episode_id": 7, "source": "asr",
                "source_path": "", "source_hash": "",
            }])
        self.assertEqual([(item["sonarrEpisodeId"], item["missing_subtitles"][0]["code2"])
                         for item in items], [(7, "id")])

    def test_current_verified_asr_row_suppresses_regen(self):
        with tempfile.TemporaryDirectory(prefix="asrsub-regen-current-") as tmp:
            stem = os.path.join(tmp, "Episode")
            source = stem + ".id.srt"
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("1\n00:00:00,000 --> 00:00:01,000\n" + o.AI_MARKER + "\nOwned.\n")
            items = self._capture_regen_items(tmp, [{
                "stem": stem, "lang": "id", "episode_id": 7, "source": "asr",
                "source_path": source, "source_hash": o.file_sha256(source),
            }])
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
