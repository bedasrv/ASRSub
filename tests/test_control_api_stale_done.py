import json
import os
import tempfile
import unittest
from unittest.mock import patch

import control_api_v2 as api2


class TestControlApiStaleDone(unittest.TestCase):
    def _api(self):
        tmp = tempfile.TemporaryDirectory(prefix="asrsub-control-stale-done-")
        media = os.path.join(tmp.name, "Show.mkv")
        open(media, "wb").close()
        state = os.path.join(tmp.name, "state.jsonl")
        with open(state, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "kind": "series", "sonarrEpisodeId": 7, "language": "id",
                "status": "done", "ts": "2026-08-01T00:00:00Z",
            }) + "\n")
            fh.write(json.dumps({
                "kind": "series", "sonarrEpisodeId": 7, "language": "jpn",
                "status": "done", "ts": "2026-08-01T00:00:00Z",
            }) + "\n")
        api = api2.ControlAPIv2({
            "STATE_FILE": state, "REGISTRY_FILE": os.path.join(tmp.name, "registry.jsonl"),
            "ENV_FILE": os.path.join(tmp.name, "pipeline.env"),
            "OVERRIDE_FILE": os.path.join(tmp.name, "overrides.json"),
        })
        wanted = {
            "total": 1, "data": [{"sonarrEpisodeId": 7, "seriesTitle": "Show",
                "episode_number": "S01E01",
                "missing_subtitles": [{"code2": "id"}]}],
        }
        detail = {"path": media, "series": "Show", "episode": "S01E01",
                  "title": "Episode", "monitored": True, "has_file": True}
        patches = [
            patch.object(api, "_bazarr_wanted", return_value=wanted),
            patch.object(api, "_ep_detail", return_value=detail),
            patch.object(api, "_daemon_status", return_value={"reachable": False}),
            patch.object(api, "_subs_cached", return_value=[]),
            patch.object(api, "_refine_latest", return_value={}),
            patch.object(api, "_read_exclusions", return_value=[]),
            patch.object(api, "_bazarr_movies", return_value={"total": 0, "data": []}),
        ]
        return tmp, api, patches

    def test_wanted_does_not_expose_stale_done_status(self):
        tmp, api, patches = self._api()
        self.addCleanup(tmp.cleanup)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            code, response = api._h_wanted(None)
        self.assertEqual(code, 200)
        item = response["items"][0]
\
        self.assertNotEqual(item["state"]["status"], "done")
        id_state = item["lang_states"].get("id")
        self.assertTrue(id_state is None or id_state["status"] != "done", item["lang_states"])
        self.assertNotIn("jpn", item["lang_states"])
        self.assertIn("ja", item["lang_states"])

    def test_library_does_not_expose_stale_done_language(self):
        tmp, api, patches = self._api()
        self.addCleanup(tmp.cleanup)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            code, response = api._h_library(None)
        self.assertEqual(code, 200)
        item = response["items"][0]
\
        languages = {entry["language"]: entry for entry in item["languages"]}
        self.assertTrue(languages.get("id", {}).get("status") != "done", languages)
        self.assertNotIn("jpn", languages)
        self.assertIn("ja", languages)


if __name__ == "__main__":
    unittest.main()
