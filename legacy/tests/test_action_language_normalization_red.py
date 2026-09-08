import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests import HermeticStateMixin

import orchestrator as o


class TestActionLanguageNormalizationRed(HermeticStateMixin):
    def test_language_scoped_retry_normalizes_alias_and_preserves_other_language(self):
        tmp = tempfile.mkdtemp(prefix="asrsub-action-red-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        actions = os.path.join(tmp, "actions.jsonl")
        state = os.path.join(tmp, "state.jsonl")
        rows = [
            {"sonarrEpisodeId": 7, "language": "ja", "status": "done"},
            {"sonarrEpisodeId": 7, "language": "id", "status": "done"},
        ]
        with open(state, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        with open(actions, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "retry",
                "episode_id": 7,
                "language": "jpn",
            }) + "\n")

        cfg = {"TARGET_LANGS": ["ja", "id"], "TMP_DIR": tmp}
        with patch.object(o, "ACTIONS_FILE", actions), patch.object(
            o, "STATE_FILE", state
        ), patch.object(o, "get_episode", return_value={}), patch.object(
            o, "_delete_episode_subtitles", return_value=[]
        ), patch.object(o, "_bazarr_wanted_refill"), patch.object(
            o.requests, "post"
        ):
            self.assertEqual(o.consume_actions(cfg), set())

        with open(state, encoding="utf-8") as fh:
            remaining = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(remaining, [rows[1]])


if __name__ == "__main__":
    unittest.main()
