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
SCRATCH = Path("/tmp/agent-scratch/asrsub-bundle-contract")


class TestFinalBundle(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(mode=0o700, parents=True, exist_ok=True)

    def test_release_tools_reject_missing_inputs_or_outputs(self):
        for tool in ("package_bundle.py", "create_approval.py"):
            result = subprocess.run(
                [PYTHON, str(ROOT / "tools" / tool), "--production"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0, tool)
            self.assertIn("requires", result.stderr.lower(), tool)

    def _run_fixture_copy(self, source: Path, manifest: Path):
        return subprocess.run(
            [
                PYTHON,
                str(ROOT / "tools" / "create_approval.py"),
                "--fixture",
                "--canonical-approval-bytes",
                str(source),
                "--approval-manifest",
                str(manifest),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def test_fixture_approval_copy_is_explicit_fixture_mode(self):
        with tempfile.TemporaryDirectory(prefix="asrsub-final-bundle-") as directory:
            root = Path(directory)
            source = root / "approval.json"
            manifest = root / "manifest.json"
            source.write_text('{"schema":"fixture-approval-v1"}\n', encoding="utf-8")
            result = self._run_fixture_copy(source, manifest)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(manifest.read_bytes(), source.read_bytes())

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
            output = root / "bundle"
            manifest = root / "bundle-manifest.json"
            args = argparse.Namespace(
                runtime_source_root=runtime,
                systemd_source_root=systemd,
                output_root=output,
                manifest_output=manifest,
                release_sha="1" * 40,
                release_sha_from_git=False,
            )
            calls = 0
            real_replace = os.replace

            def fail_second(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected publication failure")
                return real_replace(source, destination)

            with mock.patch.object(package["os"], "replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    package["package_bundle"](args, test_seam=True)
            self.assertEqual(calls, 2)
            self.assertFalse(output.exists())
            self.assertFalse(manifest.exists())
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
            calls = 0

            def fail_first(origin, destination):
                nonlocal calls
                calls += 1
                raise OSError("injected approval publication failure")

            with mock.patch.object(approval["os"], "replace", side_effect=fail_first):
                with self.assertRaises(OSError):
                    approval["_publish"](
                        manifest,
                        original,
                    )
            self.assertEqual(calls, 1)
            self.assertFalse(manifest.exists())
            self.assertEqual(list(root.glob(".*stage-*")), [])

    def test_release_workflow_remains_image_only(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("docker/build-push-action", workflow)
        self.assertIn("release.json", workflow)
        self.assertNotIn("tools/package_bundle.py", workflow)
        self.assertNotIn("tools/create_approval.py", workflow)

    def test_installer_requires_unsigned_integrity_mode(self):
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
                        "integrity_mode": "legacy",
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
                    expected_integrity_mode="unsigned",
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
                "integrity_mode": "unsigned",
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
            args = argparse.Namespace(
                canonical_approval_bytes=Path("release/approval-canonical.json"),
                approval_manifest=Path("release/approval.json"),
                release_sha_from_git=True,
            )
            old_cwd = Path.cwd()
            try:
                os.chdir(repository)
                with mock.patch.dict(os.environ, {"RELEASE_SHA": "f" * 40}, clear=False):
                    with self.assertRaises(ValueError):
                        approval["create_approval"](args, test_seam=False)
                self.assertFalse((release / "approval.json").exists())
                value["release_sha"] = head
                source.write_bytes(approval["canonical"](value) + b"\n")
                with mock.patch.dict(os.environ, {"RELEASE_SHA": "f" * 40}, clear=False):
                    with self.assertRaises(ValueError):
                        approval["create_approval"](args, test_seam=False)
                self.assertEqual(approval["create_approval"](args, test_seam=False), 0)
                self.assertEqual(json.loads((release / "approval.json").read_text())["release_sha"], head)
            finally:
                os.chdir(old_cwd)

    def test_production_builders_reject_absolute_paths_outside_fixed_release_layout(self):
        package = runpy.run_path(str(ROOT / "tools" / "package_bundle.py"))
        approval = runpy.run_path(str(ROOT / "tools" / "create_approval.py"))
        absolute_root = Path("/tmp") / "asrsub-arbitrary-production-path"
        package_args = argparse.Namespace(
            runtime_source_root=absolute_root / "runtime",
            systemd_source_root=absolute_root / "systemd",
            output_root=absolute_root / "bundle",
            manifest_output=absolute_root / "manifest.json",
            release_sha="1" * 40,
            release_sha_from_git=False,
        )
        with self.assertRaises(ValueError):
            package["package_bundle"](package_args, test_seam=False)
        approval_args = argparse.Namespace(
            canonical_approval_bytes=absolute_root / "approval-canonical.json",
            approval_manifest=absolute_root / "approval.json",
            release_sha_from_git=True,
        )
        with self.assertRaises(ValueError):
            approval["create_approval"](approval_args, test_seam=False)


if __name__ == "__main__":
    unittest.main()
