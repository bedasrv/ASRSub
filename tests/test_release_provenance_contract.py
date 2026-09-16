import re, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class TestCICDGHCRContract(unittest.TestCase):
    def test_release_has_no_latest_alias(self):
        text=(ROOT/".github/workflows/release.yml").read_text()
        self.assertNotIn(":latest", text)
        self.assertRegex(text, r"uses: [^@\n]+@[0-9a-f]{40}")
