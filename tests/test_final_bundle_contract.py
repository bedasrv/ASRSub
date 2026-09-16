import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class TestFinalBundle(unittest.TestCase):
    def test_signer_policy_binds_real_production_commands_and_key_fds(self):
        policy = json.loads((ROOT / "tools" / "signer_argv_policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["schema"], "signer-argv-policy-v2")
        for name, fd in (("bundle", "3"), ("approval", "4")):
            command = policy[name]["command"]
            self.assertIn("--production", command)
            self.assertIn(fd, command)
            self.assertNotIn("--fixture", command)
            self.assertEqual(command[0], "/usr/bin/python3")

    def test_production_approval_without_required_inputs_fails_closed(self):
        result = subprocess.run(
            [PYTHON, str(ROOT / "tools" / "create_approval.py"), "--production"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("canonical bytes", result.stderr.lower())

    def test_fixture_signing_is_marked_by_explicit_fixture_mode(self):
        with tempfile.TemporaryDirectory(prefix="asrsub-final-bundle-") as directory:
            root = Path(directory)
            source = root / "approval.json"
            manifest = root / "manifest.json"
            signature = root / "approval.sig"
            source.write_text('{"schema":"fixture-approval-v1"}\n', encoding="utf-8")
            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "tools" / "create_approval.py"),
                    "--fixture",
                    "--canonical-approval-bytes",
                    str(source),
                    "--approval-manifest",
                    str(manifest),
                    "--approval-signature",
                    str(signature),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(signature.read_bytes().startswith(b"fixture-"))
            self.assertEqual(manifest.read_bytes(), source.read_bytes())

    def test_bundle_parser_requires_explicit_release_inputs(self):
        result = subprocess.run(
            [PYTHON, str(ROOT / "tools" / "package_bundle.py"), "--production"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime-source-root", result.stderr)


if __name__ == "__main__":
    unittest.main()
