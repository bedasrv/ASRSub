import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests import HermeticStateMixin

import orchestrator as o


class TestRegistryWriteSerialization(HermeticStateMixin):
    def test_concurrent_registry_upserts_are_locked_and_valid_jsonl(self):
        tmp = tempfile.mkdtemp(prefix="asrsub-registry-lock-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        registry = os.path.join(tmp, "subtitle_registry.jsonl")
        rows = [
            (os.path.join(tmp, f"Show{i}"), i, f"hash-{i}")
            for i in range(24)
        ]

        def write(row):
            stem, episode_id, digest = row
            return o.registry_upsert(
                stem, "id", "asr", ep_id=episode_id,
                source_path=f"{stem}.id.srt", source_hash=digest,
            )

        with patch.object(o, "REGISTRY_FILE", registry):
            with ThreadPoolExecutor(max_workers=12) as pool:
                written = list(pool.map(write, rows))
            with open(registry, encoding="utf-8") as fh:
                serialized = [json.loads(line) for line in fh if line.strip()]

        self.assertEqual(len(written), len(rows))
        self.assertEqual(len(serialized), len(rows))
        self.assertEqual(
            {row["stem"] for row in serialized},
            {stem for stem, _episode_id, _digest in rows},
        )
        self.assertEqual(
            {row["episode_id"] for row in serialized},
            {episode_id for _stem, episode_id, _digest in rows},
        )
        self.assertTrue(
            os.path.exists(registry + ".lock"),
            "registry writes/deletes need a stable shared lock path",
        )


if __name__ == "__main__":
    unittest.main()
