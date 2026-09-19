import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent

class TestComposeContract(unittest.TestCase):
    def test_secret_sources_are_staged(self):
        text=(ROOT/"fixtures/compose_runtime/compose.yaml").read_text()
        self.assertIn("runtime-secrets/discord_webhook", text)
        self.assertEqual(text.count("/run/secrets/"), 1)
        self.assertNotIn("/home/user/.config/asr-pipeline/secrets", text)
