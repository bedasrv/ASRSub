import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class TestFinalBundle(unittest.TestCase):
    def _policy(self):
        return json.loads((ROOT / "tools" / "signer_argv_policy.json").read_text(encoding="utf-8"))

    def test_signer_policy_is_executable_and_binds_all_release_paths(self):
        policy = self._policy()
        self.assertEqual(policy["schema"], "signer-argv-policy-v2")
        expected = {
            "bundle": {
                "fd": "3",
                "script": "tools/package_bundle.py",
                "paths": {
                    "--runtime-source-root": "release/runtime",
                    "--systemd-source-root": "release/systemd",
                    "--output-root": "release/asrsub-runtime-bundle",
                    "--manifest-output": "release/bundle-manifest.json",
                    "--signature-output": "release/bundle-manifest.sig",
                },
                "flags": ("--production", "--release-sha-from-git", "--key-fd"),
            },
            "approval": {
                "fd": "4",
                "script": "tools/create_approval.py",
                "paths": {
                    "--canonical-approval-bytes": "release/approval-canonical.json",
                    "--approval-manifest": "release/approval.json",
                    "--approval-signature": "release/approval.sig",
                },
                "flags": ("--production", "--approval-key-fd"),
            },
        }
        for name, contract in expected.items():
            command = policy[name]["command"]
            fd = contract["fd"]
            self.assertEqual(policy[name]["launcher"], "tools/asrsub-env")
            self.assertEqual(policy[name]["target_fd"], int(fd))
            self.assertEqual(command[:3], ["tools/asrsub-env", "--pass-fd", fd])
            self.assertEqual(command[3:5], ["/usr/bin/python3", contract["script"]])
            self.assertNotIn("--fixture", command)
            for flag in contract["flags"]:
                self.assertEqual(command.count(flag), 1)
                self.assertIn(flag, command)
            self.assertEqual(command[command.index("--key-fd" if name == "bundle" else "--approval-key-fd") + 1], fd)
            for flag, value in contract["paths"].items():
                self.assertEqual(command[command.index(flag) + 1], value)

    def _run_policy_without_release(self, name):
        policy = self._policy()
        fd = policy[name]["target_fd"]
        output_paths = {
            "bundle": (
                "release/asrsub-runtime-bundle",
                "release/bundle-manifest.json",
                "release/bundle-manifest.sig",
            ),
            "approval": ("release/approval.json", "release/approval.sig"),
        }[name]
        with tempfile.TemporaryDirectory(prefix="asrsub-signer-policy-") as directory:
            workspace = Path(directory)
            tools = workspace / "tools"
            tools.mkdir()
            for tool in ("asrsub-env", "package_bundle.py", "create_approval.py"):
                shutil.copy2(ROOT / "tools" / tool, tools / tool)
            placeholder = workspace / "fd-input"
            placeholder.write_bytes(b"not-a-private-key\n")
            source_fd = os.open(placeholder, os.O_RDONLY)
            try:
                if source_fd != fd:
                    os.dup2(source_fd, fd)
                    os.close(source_fd)
                result = subprocess.run(
                    policy[name]["command"],
                    cwd=workspace,
                    pass_fds=(fd,),
                    capture_output=True,
                    text=True,
                    env={**os.environ, "RELEASE_SHA": "f" * 40},
                )
            finally:
                os.close(fd)
            outputs = {relative: (workspace / relative).exists() for relative in output_paths}
            return result, outputs

    def test_policy_bundle_fails_closed_without_release_inputs_or_outputs(self):
        result, outputs = self._run_policy_without_release("bundle")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required release input directory is absent", result.stderr.lower())
        for relative, exists in outputs.items():
            self.assertFalse(exists, relative)

    def test_policy_approval_fails_closed_without_canonical_input_or_outputs(self):
        result, outputs = self._run_policy_without_release("approval")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("canonical approval is absent", result.stderr.lower())
        for relative, exists in outputs.items():
            self.assertFalse(exists, relative)

    def test_release_sha_from_git_is_strict_and_does_not_use_ambient_value(self):
        with tempfile.TemporaryDirectory(prefix="asrsub-release-sha-") as directory:
            repository = Path(directory)
            subprocess.run(["/usr/bin/git", "init", "-q", str(repository)], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(repository), "config", "user.email", "test@test.local"], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(repository), "config", "user.name", "test"], check=True)
            (repository / "release-marker").write_text("release\n", encoding="utf-8")
            subprocess.run(["/usr/bin/git", "-C", str(repository), "add", "release-marker"], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(repository), "commit", "-qm", "release"], check=True)
            expected = subprocess.check_output(["/usr/bin/git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
            module = runpy.run_path(str(ROOT / "tools" / "package_bundle.py"))
            with mock.patch.dict(os.environ, {"RELEASE_SHA": "f" * 40}):
                actual = module["release_sha_from_git"](repository)
            self.assertEqual(actual, expected)
            self.assertRegex(actual, r"^[0-9a-f]{40}$")

    def test_systemd_inventory_is_fixture_only_and_marker_is_required_by_harness(self):
        inventory_path = ROOT / "tests" / "fixtures" / "systemd" / "runtime-bundle-inventory.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        self.assertIs(inventory.get("fixture_only"), True)
        with tempfile.TemporaryDirectory(prefix="asrsub-systemd-fixture-") as directory:
            temporary = Path(directory)
            fixture = temporary / "systemd"
            shutil.copytree(ROOT / "tests" / "fixtures" / "systemd", fixture)
            shutil.copytree(ROOT / "tests" / "fixtures" / "common", temporary / "common")
            unmarked = json.loads((fixture / "runtime-bundle-inventory.json").read_text(encoding="utf-8"))
            unmarked.pop("fixture_only")
            (fixture / "runtime-bundle-inventory.json").write_text(json.dumps(unmarked), encoding="utf-8")
            result = subprocess.run(
                [PYTHON, str(fixture / "run_fixture.py"), "test_provisional_bundle_contains_units_and_dropin"],
                cwd=fixture,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("fixture_only", result.stderr + result.stdout)

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
