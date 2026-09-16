import json,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class TestJournalSchema(unittest.TestCase):
 def test_canonical_vectors(self): self.assertEqual(json.loads((ROOT/"tests/fixtures/deploy_journal/canonical_vectors.json").read_text())["schema"],"deployment-canonical-vector-set-v1")
 def test_canonical_vector_command_projection(self): self.assertTrue((ROOT/"tests/fixtures/deploy_journal/journal_vectors.json").exists())
