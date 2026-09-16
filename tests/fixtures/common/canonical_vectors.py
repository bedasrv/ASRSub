"""Reader for the Rust-owned canonical vector projection."""
import json
from pathlib import Path

def load(repo_root: Path):
    return json.loads((repo_root / "tests/fixtures/canonical_vectors.json").read_text())
