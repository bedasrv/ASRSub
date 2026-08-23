import unittest
from unittest.mock import patch, MagicMock

import orchestrator as o


class TestJapaneseEchoGuard(unittest.TestCase):
    def test_attempt_chunk_returns_for_ja_with_kana(self):
        """Bug1: _attempt_chunk must NOT reject kana/kanji output for Japanese targets."""
        fake_parsed = {1: "大丈夫だよ。彼に聞いてごらん、デンデ。", 2: "次の行です。"}
        with patch.object(o, "_local_chunk", return_value=fake_parsed) as lc:
            parsed = o._attempt_chunk({}, ["orig1", "orig2"], "Japanese", "k")
            self.assertIsNotNone(parsed, "_attempt_chunk returned None for Japanese kana output — echo guard should be skipped")
            self.assertEqual(parsed[1], fake_parsed[1])

    def test_attempt_chunk_still_rejects_cjk_for_non_ja(self):
        """Regression: non-Japanese targets must still reject CJK echo."""
        fake_parsed = {1: "エコーしてしまった日本語", 2: "other"}
        with patch.object(o, "_local_chunk", return_value=fake_parsed):
            parsed = o._attempt_chunk({}, ["orig1", "orig2"], "Indonesian", "k")
            self.assertIsNone(parsed, "non-Japanese CJK output should be rejected as echo")


class TestAsrGuardSkip(unittest.TestCase):
    def _run_process_after_asr(self, asr_lang, expected_skip):
        import tempfile, os, json
        cfg = {
            "TRANSLATE_BASE": "http://127.0.0.1:8011/v1",
            "TRANSLATE_MODEL": o.GEMMA_MODEL,
        }
        cues = [{"start": 0, "end": 1000, "text": "hello world this is a long english dialogue line for testing"}]
        decision = {"needs_translate": True, "asr_lang": asr_lang}
        info = {"seriesId": 1, "episodeFile": {"path": "/tmp/fake.mkv"}, "title": "T"}
        captured = {}

        def fake_translate_texts(cfg_, cues_, target_lang, key, series_id=None, episode_id=None, prior_cache=None, series_title=None, skip_guard=False):
            captured["skip_guard"] = skip_guard
            # return minimal groups so process_after_asr can finish
            return [(0, 1, "translated line")]

        with patch.object(o, "translate_texts", side_effect=fake_translate_texts) as tt:
            with patch.object(o, "write_srt", return_value=None):
                with patch.object(o, "upload_srt", return_value=204):
                    with patch.object(o, "upload_srt_movie", return_value=204):
                        with patch.object(o, "jellyfin_refresh", return_value=None):
                            with patch.object(o, "registry_upsert", return_value=None):
                                with patch.object(o, "append_state", return_value=None):
                                    with patch.object(o, "notify_webhook", return_value=None):
                                        with patch.object(o, "notify_hermes", return_value=None):
                                            with patch.object(o, "halt_on_error", return_value=None):
                                                with patch.object(o, "map_path", return_value="/tmp/fake.mkv"):
                                                    # need to patch os.path.isfile? process_after_asr checks media_path via map_path already mocked; but also it will try to map inside if needed
                                                    # we mock file reads: write_srt creates srt_path, but we avoid reading; patch open
                                                    # Instead patch open for reading srt_bytes
                                                    real_open = open
                                                    def fake_open(path, mode="r", *a, **kw):
                                                        if "rb" in mode:
                                                            mock = MagicMock()
                                                            mock.__enter__ = lambda s: s
                                                            mock.__exit__ = lambda *a: False
                                                            mock.read = lambda: b"fake srt"
                                                            return mock
                                                        return real_open(path, mode, *a, **kw)
                                                    # patch builtins open locally
                                                    with patch("builtins.open", side_effect=fake_open):
                                                        # need cps_merge to pass through
                                                        with patch.object(o, "cps_merge", side_effect=lambda x: x):
                                                            res = o.process_after_asr(cfg, "k", 99, "id", "Series", "S01E01", "ja", 0, cues, decision, info, "/tmp/f.wav", "/tmp/f.srt")
                                                    # assert skip_guard was as expected
                                                    self.assertIn("skip_guard", captured, "translate_texts not called")
                                                    self.assertEqual(captured["skip_guard"], expected_skip,
                                                                     f"skip_guard for asr_lang={asr_lang} should be {expected_skip}")

    def test_skip_guard_true_for_english_asr(self):
        self._run_process_after_asr("en", True)

    def test_skip_guard_false_for_japanese_asr(self):
        self._run_process_after_asr("ja", False)
