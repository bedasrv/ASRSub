import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
SHA = "1" * 40
DIGEST = "ghcr.io/bedasrv/asrsub@sha256:" + "a" * 64


class TestRolloutReceipt(unittest.TestCase):
    def test_fixture_receipt_has_explicit_non_evidence_schema(self):
        with tempfile.TemporaryDirectory(prefix="asrsub-rollout-contract-") as directory:
            root = Path(directory)
            fixture = root / "fixture.json"
            output = root / "receipt.json"
            fixture.write_text('{"schema":"fixture-v1","result":"success"}\n', encoding="utf-8")
            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "tools" / "record_rollout.py"),
                    "--fixture",
                    str(fixture),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                set(receipt),
                {"schema", "kind", "result", "evidence_paths", "evidence_eligible"},
            )
            self.assertEqual(receipt["schema"], "rollout-receipt-v1")
            self.assertEqual(receipt["kind"], "fixture")
            self.assertEqual(receipt["result"], "success")
            self.assertFalse(receipt["evidence_eligible"])
            self.assertEqual(receipt["evidence_paths"], [])

    def test_approval_binding_keeps_full_reference_and_bare_digest_distinct(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import record_rollout  # noqa: PLC0415
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="asrsub-approval-contract-") as directory:
            path = Path(directory) / "approval.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "approval-v1",
                        "integrity_mode": "unsigned",
                        "generation": 1,
                        "release_sha": SHA,
                        "bundle_sha256": "b" * 64,
                        "image_digest": "a" * 64,
                        "approved_docker_socket": "default",
                        "approved_state_root": "/var/lib/asrsub/state",
                    }
                ),
                encoding="utf-8",
            )
            binding = record_rollout._approval_evidence(
                path,
                release_sha=SHA,
                image_digest=DIGEST,
                bundle_sha="b" * 64,
                production=True,
            )
            self.assertEqual(binding["image_ref"], DIGEST)
            self.assertEqual(binding["image_digest"], "a" * 64)
            mismatch = json.loads(path.read_text(encoding="utf-8"))
            mismatch["image_digest"] = "c" * 64
            path.write_text(json.dumps(mismatch), encoding="utf-8")
            with self.assertRaises(record_rollout.AdapterError):
                record_rollout._approval_evidence(
                    path,
                    release_sha=SHA,
                    image_digest=DIGEST,
                    bundle_sha="b" * 64,
                    production=True,
                )

    def test_production_receipt_contract_rejects_unbound_record_identity(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import record_rollout  # noqa: PLC0415
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="asrsub-record-contract-") as directory:
            root = Path(directory)
            receipt = root / "receipt.json"
            receipt.write_text(
                json.dumps(
                    {
                        "schema": "runtime-bundle-install-receipt-v1",
                        "record_authority": "non-authoritative-install-record-v1",
                        "dry_run": False,
                        "evidence_eligible": True,
                        "release_sha": SHA,
                        "manifest_sha256": "b" * 64,
                        "target_root": str(root / "runtime"),
                        "systemd_root": "/etc/systemd/system",
                        "target_identity": {"device": 1, "inode": 2, "mount_id": 3, "filesystem": "ext4"},
                        "approval": {"verified": True, "release_sha": "wrong"},
                        "members": [],
                    }
                ),
                encoding="utf-8",
            )
            # The production path must never accept this ordinary hand-authored
            # record as evidence; the missing installed tree is an intentional
            # first gate, not an authenticated receipt shortcut.
            args = type("Args", (), {"bundle_receipt": receipt})()
            with self.assertRaises(record_rollout.AdapterError):
                record_rollout._bundle_evidence(args, root / "runtime", "b" * 64, SHA, production=True)


if __name__ == "__main__":
    unittest.main()
