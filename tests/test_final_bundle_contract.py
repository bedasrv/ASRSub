import argparse
import hashlib
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
SCRATCH = Path("/tmp/agent-scratch/asrsub-signer-correction")


class TestFinalBundle(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _policy(self):
        return json.loads((ROOT / "tools" / "signer_argv_policy.json").read_text(encoding="utf-8"))

    def test_signer_policy_is_executable_and_binds_all_release_paths(self):
        policy = self._policy()
        self.assertEqual(policy["schema"], "signer-argv-policy-v2")
        self.assertEqual(policy["working_directory"], "repository-root")
        self.assertEqual(policy["cwd"], ".")
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
                "flags": ("--production", "--release-sha-from-git", "--approval-key-fd"),
            },
        }
        for name, contract in expected.items():
            command = policy[name]["command"]
            fd = contract["fd"]
            self.assertEqual(policy[name]["launcher"], "tools/asrsub-env")
            self.assertEqual(policy[name]["target_fd"], int(fd))
            self.assertEqual(command[:4], ["/usr/bin/python3", "tools/asrsub-env", "--pass-fd", fd])
            self.assertEqual(command[4:6], ["/usr/bin/python3", contract["script"]])
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
        self.assertTrue(
            "required release input directory is absent" in result.stderr.lower()
            or "repository root" in result.stderr.lower()
        )
        for relative, exists in outputs.items():
            self.assertFalse(exists, relative)

    def test_policy_approval_fails_closed_without_canonical_input_or_outputs(self):
        result, outputs = self._run_policy_without_release("approval")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            "canonical approval is absent" in result.stderr.lower()
            or "repository root" in result.stderr.lower()
        )
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

    def _release_sources(self, root, package):
        runtime = root / "runtime"
        systemd = root / "systemd"
        runtime.mkdir()
        systemd.mkdir()
        for name in package["RUNTIME_MEMBERS"]:
            path = runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("runtime:" + name + "\n").encode())
            path.chmod(package["MEMBER_MODES"][name])
        for name in package["SYSTEMD_MEMBERS"]:
            path = systemd / name.removeprefix("systemd/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("systemd:" + name + "\n").encode())
            path.chmod(0o644)
        return runtime, systemd

    def _capture_then_replace(self, module, source, decoy, capture):
        source_identity = (source.stat().st_dev, source.stat().st_ino)
        real_read = module["os"].read
        real_fstat = module["os"].fstat
        replaced = False

        def replace_after_read(file_fd, size):
            nonlocal replaced
            block = real_read(file_fd, size)
            if not replaced:
                fd_stat = real_fstat(file_fd)
                if (fd_stat.st_dev, fd_stat.st_ino) == source_identity:
                    source.unlink()
                    source.symlink_to(decoy)
                    replaced = True
            return block

        with mock.patch.object(module["os"], "read", side_effect=replace_after_read):
            result = capture()
        self.assertTrue(replaced, "the low-level capture read seam was not exercised")
        return result

    def test_bundle_manifest_uses_captured_bytes_after_path_replacement(self):
        package = runpy.run_path(str(ROOT / "tools" / "package_bundle.py"))
        with tempfile.TemporaryDirectory(prefix="asrsub-capture-", dir=SCRATCH) as directory:
            root = Path(directory)
            runtime, systemd = self._release_sources(root, package)
            victim = runtime / "asrsub"
            original = victim.read_bytes()
            decoy = root / "decoy"
            decoy.write_bytes(b"decoy\n")
            manifest, _members = self._capture_then_replace(
                package, victim, decoy, lambda: package["_manifest"](runtime, systemd, "1" * 40)
            )
            member = next(item for item in manifest["members"] if item["path"] == "asrsub")
            self.assertEqual(member["sha256"], hashlib.sha256(original).hexdigest())

            victim.unlink()
            victim.write_bytes(original)
            victim.chmod(package["MEMBER_MODES"]["asrsub"])
            victim.unlink()
            victim.symlink_to(decoy)
            with self.assertRaises(ValueError):
                package["_manifest"](runtime, systemd, "1" * 40)

    def test_bundle_publication_failure_removes_every_final_and_stage_artifact(self):
        package = runpy.run_path(str(ROOT / "tools" / "package_bundle.py"))
        with tempfile.TemporaryDirectory(prefix="asrsub-publication-", dir=SCRATCH) as directory:
            root = Path(directory)
            runtime, systemd = self._release_sources(root, package)
            key = root / "signing-key.pem"
            subprocess.run(
                ["/usr/bin/openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            key_fd = os.open(key, os.O_RDONLY)
            output = root / "bundle"
            manifest = root / "bundle-manifest.json"
            signature = root / "bundle-manifest.sig"
            args = argparse.Namespace(
                runtime_source_root=runtime,
                systemd_source_root=systemd,
                output_root=output,
                manifest_output=manifest,
                signature_output=signature,
                release_sha="1" * 40,
                release_sha_from_git=False,
                key_fd=key_fd,
            )
            calls = 0
            real_replace = os.replace

            def fail_third(source, destination):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError("injected publication failure")
                return real_replace(source, destination)

            try:
                with mock.patch.object(package["os"], "replace", side_effect=fail_third):
                    with self.assertRaises(OSError):
                        package["package_bundle"](args, test_seam=True)
            finally:
                os.close(key_fd)
            self.assertEqual(calls, 3)
            self.assertFalse(output.exists())
            self.assertFalse(manifest.exists())
            self.assertFalse(signature.exists())
            self.assertEqual(list(root.glob(".*stage-*")), [])

    def test_approval_capture_and_publication_are_no_follow_and_fail_closed(self):
        approval = runpy.run_path(str(ROOT / "tools" / "create_approval.py"))
        with tempfile.TemporaryDirectory(prefix="asrsub-approval-", dir=SCRATCH) as directory:
            root = Path(directory)
            source = root / "approval.json"
            decoy = root / "decoy.json"
            source.write_bytes(b'{"schema":"fixture-approval-v1"}\n')
            decoy.write_bytes(b'{"schema":"decoy-approval-v1"}\n')
            original = source.read_bytes()
            value, raw = self._capture_then_replace(
                approval, source, decoy, lambda: approval["_read_canonical"](source)
            )
            self.assertEqual(value["schema"], "fixture-approval-v1")
            self.assertEqual(raw, original)

            with self.assertRaises(ValueError):
                approval["_read_canonical"](source)
            source.unlink()
            source.write_bytes(original)

            manifest = root / "manifest.json"
            signature = root / "approval.sig"
            calls = 0
            real_replace = os.replace

            def fail_second(origin, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected approval publication failure")
                return real_replace(origin, destination)

            with mock.patch.object(approval["os"], "replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    approval["fixture_approval"](
                        argparse.Namespace(
                            canonical_approval_bytes=source,
                            approval_manifest=manifest,
                            approval_signature=signature,
                        )
                    )
            self.assertEqual(calls, 2)
            self.assertFalse(manifest.exists())
            self.assertFalse(signature.exists())
            self.assertEqual(list(root.glob(".*stage-*")), [])

    def test_release_workflow_remains_image_only_and_does_not_launch_signers(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        for forbidden in ("package_bundle.py", "create_approval.py", "SIGNING_KEY", "APPROVAL_KEY"):
            self.assertNotIn(forbidden, workflow)

    def test_production_installer_rejects_a_test_seam_manifest(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            installer = runpy.run_path(str(ROOT / "tools" / "install_runtime_bundle.py"))
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="asrsub-mode-", dir=SCRATCH) as directory:
            root = Path(directory)
            bundle = root / "bundle"
            bundle.mkdir()
            manifest = bundle / "manifest.json"
            manifest.write_bytes(
                json.dumps(
                    {
                        "schema": "runtime-bundle-manifest-v1",
                        "signing_mode": "test-seam",
                        "release_sha": "1" * 40,
                        "members": [{"path": "asrsub", "sha256": "a" * 64, "mode": "0755"}],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            with self.assertRaises(Exception):
                installer["_load_manifest"](
                    bundle,
                    manifest,
                    "1" * 40,
                    expected_signing_mode="production",
                )

    def test_production_approval_binds_canonical_release_to_git_not_ambient_env(self):
        approval = runpy.run_path(str(ROOT / "tools" / "create_approval.py"))
        with tempfile.TemporaryDirectory(prefix="asrsub-approval-git-", dir=SCRATCH) as directory:
            repository = Path(directory)
            subprocess.run(["/usr/bin/git", "init", "-q", str(repository)], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(repository), "config", "user.email", "test@test.local"], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(repository), "config", "user.name", "test"], check=True)
            (repository / "marker").write_text("release\n", encoding="utf-8")
            subprocess.run(["/usr/bin/git", "-C", str(repository), "add", "marker"], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(repository), "commit", "-qm", "release"], check=True)
            head = subprocess.check_output(["/usr/bin/git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
            release = repository / "release"
            release.mkdir()
            value = {
                "schema": "approval-v1",
                "signing_mode": "production",
                "generation": 1,
                "release_sha": "2" * 40,
                "bundle_sha256": "a" * 64,
                "image_digest": "b" * 64,
                "compose_sha256": "c" * 64,
                "compose_template_sha256": "d" * 64,
                "notifications_enabled": False,
                "approved_docker_socket": "default",
                "approved_state_root": "/var/lib/asrsub/state",
                "created_epoch_ns": 0,
            }
            source = release / "approval-canonical.json"
            source.write_bytes(approval["canonical"](value) + b"\n")
            key = repository / "key.pem"
            subprocess.run(
                ["/usr/bin/openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            key_fd = os.open(key, os.O_RDONLY)
            args = argparse.Namespace(
                canonical_approval_bytes=Path("release/approval-canonical.json"),
                approval_manifest=Path("release/approval.json"),
                approval_signature=Path("release/approval.sig"),
                approval_key_fd=key_fd,
                release_sha_from_git=True,
            )
            old_cwd = Path.cwd()
            try:
                os.chdir(repository)
                with mock.patch.dict(os.environ, {"RELEASE_SHA": "f" * 40}, clear=False):
                    with self.assertRaises(ValueError):
                        approval["sign_approval"](args, test_seam=False)
                self.assertFalse((release / "approval.json").exists())
                self.assertFalse((release / "approval.sig").exists())
                value["release_sha"] = head
                source.write_bytes(approval["canonical"](value) + b"\n")
                with mock.patch.dict(os.environ, {"RELEASE_SHA": "f" * 40}, clear=False):
                    with self.assertRaises(ValueError):
                        approval["sign_approval"](args, test_seam=False)
                self.assertEqual(approval["sign_approval"](args, test_seam=False), 0)
                self.assertEqual(json.loads((release / "approval.json").read_text())["release_sha"], head)
            finally:
                os.chdir(old_cwd)
                os.close(key_fd)

    def test_production_signers_reject_absolute_paths_outside_fixed_release_layout(self):
        package = runpy.run_path(str(ROOT / "tools" / "package_bundle.py"))
        approval = runpy.run_path(str(ROOT / "tools" / "create_approval.py"))
        absolute_root = Path("/tmp") / "asrsub-arbitrary-production-path"
        package_args = argparse.Namespace(
            runtime_source_root=absolute_root / "runtime",
            systemd_source_root=absolute_root / "systemd",
            output_root=absolute_root / "bundle",
            manifest_output=absolute_root / "manifest.json",
            signature_output=absolute_root / "manifest.sig",
            release_sha="1" * 40,
            release_sha_from_git=False,
            key_fd=0,
        )
        with self.assertRaises(ValueError):
            package["package_bundle"](package_args, test_seam=False)
        approval_args = argparse.Namespace(
            canonical_approval_bytes=absolute_root / "approval-canonical.json",
            approval_manifest=absolute_root / "approval.json",
            approval_signature=absolute_root / "approval.sig",
            approval_key_fd=0,
            release_sha_from_git=True,
        )
        with self.assertRaises(ValueError):
            approval["sign_approval"](approval_args, test_seam=False)


if __name__ == "__main__":
    unittest.main()
