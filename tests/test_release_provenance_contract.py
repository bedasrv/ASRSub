import re, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class TestCICDGHCRContract(unittest.TestCase):
    def test_release_publishes_sha_and_main_latest_alias(self):
        text=(ROOT/".github/workflows/release.yml").read_text()
        tags=text.split("          tags: |", 1)[1].split("          cache-from:", 1)[0]
        self.assertIn("${{ env.IMAGE_NAME }}:${{ github.sha }}", tags)
        self.assertIn("github.ref == 'refs/heads/main'", tags)
        self.assertIn("format('{0}:latest', env.IMAGE_NAME)", tags)
        self.assertRegex(text, r"uses: [^@\n]+@[0-9a-f]{40}")
