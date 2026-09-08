import unittest
from unittest.mock import patch

import orchestrator as o


class TestTranslationMergeFallbackSourceLanguage(unittest.TestCase):
    def test_per_line_fallback_preserves_source_language(self):
        calls = []

        def fail_attempt(*args, **kwargs):
            calls.append(kwargs)
            return None

        with patch.object(o, "_attempt_chunk", side_effect=fail_attempt):
            result = o._translate_merge_aware(
                {}, ["first line", "second line"], "Indonesian", "key",
                source_lang="English",
            )

        self.assertEqual(result, [(0, 1, ""), (1, 2, "")])
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["source_lang"], "English")
        self.assertTrue(
            all(kwargs.get("source_lang") == "English" for kwargs in calls[1:])
        )


if __name__ == "__main__":
    unittest.main()
