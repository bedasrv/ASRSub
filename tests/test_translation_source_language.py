import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests import HermeticStateMixin

import orchestrator as o


class TestTranslationSourceLanguage(HermeticStateMixin):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="asrsub-source-language-")

    def test_gemma_prompt_serializes_actual_source_language(self):
        _system, user = o._gemma_prompt(
            ["hello"], "Indonesian", source_lang="English"
        )
        payload = json.loads(user)
        self.assertEqual(payload["source_language"], "English")
        self.assertEqual(payload["target_language"], "Indonesian")

    def test_cloud_translation_payload_contains_actual_source_language(self):
        captured = []

        def post_chat(_cfg, messages, *_args, **_kwargs):
            captured.extend(messages)
            return '["halo"]'

        cfg = {"TRANSLATE_BASE": "http://translate.example/v1"}
        with patch.object(o, "post_chat", side_effect=post_chat):
            result = o.chat_translate_batch(
                cfg, ["hello"], "id", "key", source_lang="English"
            )
        self.assertEqual(result, ["halo"])
        self.assertTrue(any("English" in str(message) for message in captured))
        user = next(message["content"] for message in captured if message["role"] == "user")
        self.assertEqual(json.loads(user)["source_language"], "English")

    def test_local_merge_translation_propagates_source_language(self):
        captured = []

        def post_chat(_cfg, messages, *_args, **_kwargs):
            captured.extend(messages)
            return '["halo"]'

        with patch.object(o, "post_chat", side_effect=post_chat):
            result = o._translate_merge_aware(
                {"TRANSLATE_MODEL": "local"}, ["hello"], "Indonesian", "key",
                source_lang="English",
            )
        self.assertEqual(result, [(0, 1, "halo")])
        user = next(message["content"] for message in captured if message["role"] == "user")
        self.assertEqual(json.loads(user)["source_language"], "English")

    def test_process_ladder_passes_english_source_language_to_translation(self):
        media = os.path.join(self.tmp, "Show.mkv")
        source_path = os.path.join(self.tmp, "Show.eng.srt")
        open(media, "wb").close()
        with open(source_path, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
        captured = {}

        def translate(*_args, **kwargs):
            captured.update(kwargs)
            return [(0, 1, "halo")]

        source = {
            "kind": "eng",
            "source_path": source_path,
            "source_hash": o.file_sha256(source_path),
            "cues": [{"start": 0, "end": 1000, "text": "hello"}],
            "tmp": False,
        }
        info = {"episodeFile": {"path": media}, "seriesId": 4, "title": "Show"}
        with patch.object(o, "REGISTRY_FILE", os.path.join(self.tmp, "registry.jsonl")), patch.object(
            o, "translate_texts", side_effect=translate
        ), patch.object(o, "upload_srt", return_value=204), patch.object(
            o, "jellyfin_refresh"
        ), patch.object(o, "notify_webhook"), patch.object(o, "append_state"):
            result = o.process_ladder(
                {"TMP_DIR": self.tmp}, "key", 12, "id", "Show", "S01E01", source, info
            )
        self.assertEqual(result, "done")
        self.assertEqual(captured["source_lang"], "English")
        target = os.path.splitext(media)[0] + ".id.hi.srt"
        self.assertTrue(os.path.isfile(target))
        with open(target, encoding="utf-8") as fh:
            self.assertIn("halo", fh.read())


if __name__ == "__main__":
    unittest.main()
