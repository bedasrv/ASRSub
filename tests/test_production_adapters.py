import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
SHA = "1" * 40
DIGEST = "ghcr.io/bedasrv/asrsub@sha256:" + "a" * 64
BUNDLE_SHA = "b" * 64


def run_tool(name, *args):
    return subprocess.run(
        [PYTHON, str(ROOT / "tools" / name), *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class AdapterTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="asrsub-prod-adapters-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_executable(self, name, body):
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def write_fake_docker(self):
        return self.write_executable(
            "docker-fake.py",
            """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

Path(os.environ["ASRSUB_ARGV_LOG"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
print(json.dumps({
    "Id": "sha256:" + "a" * 64,
    "RepoDigests": ["ghcr.io/bedasrv/asrsub@sha256:" + "a" * 64],
    "CONTROL_API_KEY": "super-secret-docker-output",
    "bare": os.environ.get("ASRSUB_SECRET", ""),
}))
""",
        )

    def make_bundle(self):
        bundle = self.root / "bundle"
        (bundle / "bin").mkdir(parents=True, exist_ok=True)
        payload = bundle / "bin" / "asrsub"
        payload.write_bytes(b"runtime-binary\n")
        payload.chmod(0o755)
        member = {
            "path": "bin/asrsub",
            "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
            "mode": "0755",
        }
        manifest = {
            "schema": "runtime-bundle-manifest-v1",
            "release_sha": SHA,
            "members": [member],
        }
        manifest_path = bundle / "manifest.json"
        manifest_path.write_bytes(canonical(manifest) + b"\n")
        approval = self.root / "approval.json"
        approval.write_text(
            json.dumps({"schema": "approval-v1", "release_sha": SHA}), encoding="utf-8"
        )
        verifier = self.write_executable(
            "verify.py",
            """#!/usr/bin/env python3
import os
import sys
from pathlib import Path
Path(os.environ.get("ASRSUB_VERIFY_LOG", "/dev/null")).write_text("verified\\n" + " ".join(sys.argv[1:]), encoding="utf-8")
print("CONTROL_API_KEY=super-secret-verifier-output")
""",
        )
        return bundle, manifest_path, approval, verifier, hashlib.sha256(canonical(manifest)).hexdigest()

    def make_rollout_inputs(self):
        deployment = self.root / "deployment"
        deployment.mkdir()
        (deployment / "state.jsonl").write_text("{}\n", encoding="utf-8")
        bundle, manifest, approval, verifier, manifest_hash = self.make_bundle()
        docker_evidence = self.root / "docker-evidence.json"
        docker_evidence.write_text(
            json.dumps(
                {
                    "schema": "docker-operation-evidence-v1",
                    "operation": "image-inspect",
                    "requested_digest": DIGEST,
                    "image_digest": DIGEST,
                    "release_sha": SHA,
                    "observed": {
                        "image_ref": DIGEST,
                        "RepoDigests": [DIGEST],
                    },
                    "stdout": "CONTROL_API_KEY=secret-from-adapter",
                }
            ),
            encoding="utf-8",
        )
        systemd = self.root / "systemd"
        (systemd / "docker.service.d").mkdir(parents=True)
        (systemd / "asrsub-recovery.service").write_text("[Unit]\n", encoding="utf-8")
        (systemd / "asrsub-runtime.service").write_text("[Unit]\n", encoding="utf-8")
        (systemd / "docker.service.d" / "asrsub-recovery.conf").write_text("[Service]\n", encoding="utf-8")
        cgroup = self.root / "cgroup"
        cgroup.mkdir()
        (cgroup / "cgroup.controllers").write_text("cpu memory pids\n", encoding="utf-8")
        (cgroup / "cgroup.procs").write_text(str(os.getpid()) + "\n", encoding="utf-8")
        return deployment, bundle, manifest, docker_evidence, systemd, cgroup, manifest_hash, approval, verifier


class TestProductionEntrypoints(AdapterTestCase):
    def test_missing_fixed_host_artifacts_block_recovery_and_runtime(self):
        for script, action in (("asrsub-recover", "--preflight"), ("asrsub-runtime", "--reconcile")):
            result = subprocess.run(
                [str(ROOT / "scripts" / script), action],
                cwd=ROOT,
                env={"PATH": os.environ.get("PATH", "")},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0, script)
            self.assertIn("blocked", (result.stdout + result.stderr).lower(), script)


class TestProductionModeAndDocker(AdapterTestCase):
    def test_production_mode_is_explicit_and_never_uses_fixture_fallback(self):
        fixture = self.root / "fixture.json"
        fixture.write_text(json.dumps({"image_ref": DIGEST}), encoding="utf-8")
        output = self.root / "inspect.json"
        no_mode = run_tool(
            "deploy_docker.py",
            "image-inspect",
            "--digest",
            DIGEST,
            "--output",
            output,
        )
        self.assertNotEqual(no_mode.returncode, 0)
        self.assertFalse(output.exists())
        ambiguous = run_tool(
            "deploy_docker.py",
            "--production",
            "image-inspect",
            "--fixture-input",
            fixture,
            "--digest",
            DIGEST,
            "--output",
            output,
        )
        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertIn("fixture", (ambiguous.stdout + ambiguous.stderr).lower())

    def test_production_rejects_injected_docker_path(self):
        fake = self.write_fake_docker()
        result = run_tool(
            "deploy_docker.py",
            "--production",
            "--docker-executable",
            fake,
            "image-inspect",
            "--digest",
            DIGEST,
            "--output",
            self.root / "evidence.json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("test seam", (result.stdout + result.stderr).lower())

    def test_test_seam_uses_exact_context_argv_and_no_shell(self):
        fake = self.write_fake_docker()
        argv_log = self.root / "argv.json"
        compose = self.root / "compose.yaml"
        compose.write_text("services: {}\n", encoding="utf-8")
        output = self.root / "evidence.json"
        env = os.environ.copy()
        env["ASRSUB_ARGV_LOG"] = str(argv_log)
        result = subprocess.run(
            [
                PYTHON,
                str(ROOT / "tools" / "deploy_docker.py"),
                "--test-seam",
                "--docker-executable",
                str(fake),
                "up",
                "--compose-file",
                str(compose),
                "--output",
                str(output),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(argv_log.read_text(encoding="utf-8"))
        self.assertEqual(argv[:2], ["--context", "default"])
        self.assertEqual(argv[2:5], ["compose", "-f", str(compose)])
        self.assertEqual(argv[5:], ["up", "-d", "--no-build", "--pull=never"])
        source = (ROOT / "tools" / "deploy_docker.py").read_text(encoding="utf-8")
        self.assertNotIn("shell=True", source)
        evidence = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(evidence["returncode"], 0)

    def test_digest_validation_and_secret_redaction(self):
        fake = self.write_fake_docker()
        output = self.root / "evidence.json"
        env = os.environ.copy()
        env["ASRSUB_ARGV_LOG"] = str(self.root / "argv.json")
        env["ASRSUB_SECRET"] = "super-secret-environment-value"
        invalid = subprocess.run(
            [
                PYTHON,
                str(ROOT / "tools" / "deploy_docker.py"),
                "--test-seam",
                "--docker-executable",
                str(fake),
                "image-inspect",
                "--digest",
                "ghcr.io/bedasrv/asrsub:latest",
                "--output",
                str(output),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertFalse(output.exists())
        valid = subprocess.run(
            [
                PYTHON,
                str(ROOT / "tools" / "deploy_docker.py"),
                "--test-seam",
                "--docker-executable",
                str(fake),
                "image-inspect",
                "--digest",
                DIGEST,
                "--output",
                str(output),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        text = output.read_text(encoding="utf-8") + valid.stdout + valid.stderr
        self.assertNotIn("super-secret-docker-output", text)
        self.assertNotIn("super-secret-environment-value", text)
        self.assertIn("<redacted>", text)


    def test_production_rejects_nonfixed_compose_path(self):
        compose = self.root / "compose.yaml"
        compose.write_text("services: {}\n", encoding="utf-8")
        result = run_tool(
            "deploy_docker.py",
            "--production",
            "compose-up",
            "--digest",
            DIGEST,
            "--compose-file",
            compose,
            "--output",
            self.root / "evidence.json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixed", (result.stdout + result.stderr).lower())

    def test_test_seam_rejects_forbidden_ambient_docker_environment(self):
        fake = self.write_fake_docker()
        compose = self.root / "compose.yaml"
        compose.write_text("services: {}\n", encoding="utf-8")
        env = os.environ.copy()
        env["ASRSUB_ARGV_LOG"] = str(self.root / "argv.json")
        env["DOCKER_HOST"] = "tcp://unapproved.example"
        result = subprocess.run(
            [
                PYTHON,
                str(ROOT / "tools" / "deploy_docker.py"),
                "--test-seam",
                "--docker-executable",
                str(fake),
                "compose-up",
                "--compose-file",
                str(compose),
                "--output",
                str(self.root / "evidence.json"),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ambient", (result.stdout + result.stderr).lower())


    def test_compose_uses_entrypoint_compatible_daemon_and_real_healthcheck(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('command: ["daemon"]', compose)
        self.assertNotIn('command: ["/usr/local/bin/asrsub"]', compose)
        self.assertRegex(compose, r"(?ms)^\s*healthcheck:\n.*test:.*?/ready")


    def test_output_write_failure_is_bounded_after_side_effect(self):
        fake = self.write_executable(
            "side-effect.py",
            """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
Path(os.environ["ASRSUB_SIDE_EFFECT"]).write_text("side effect\\n", encoding="utf-8")
print(json.dumps({"Id": "sha256:" + "a" * 64, "RepoDigests": ["ghcr.io/bedasrv/asrsub@sha256:" + "a" * 64]}))
""",
        )
        side_effect = self.root / "side-effect.marker"
        blocked_parent = self.root / "not-a-directory"
        blocked_parent.write_text("x", encoding="utf-8")
        env = os.environ.copy()
        env["ASRSUB_SIDE_EFFECT"] = str(side_effect)
        result = subprocess.run(
            [
                PYTHON,
                str(ROOT / "tools" / "deploy_docker.py"),
                "--test-seam",
                "--docker-executable",
                str(fake),
                "image-inspect",
                "--digest",
                DIGEST,
                "--output",
                blocked_parent / "evidence.json",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(side_effect.exists())
        self.assertNotIn("Traceback", result.stdout + result.stderr)


    def test_deploy_docs_require_digest_and_systemd_owned_flow(self):
        docs = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
        self.assertIn("@sha256:", docs)
        self.assertIn("asrsub-recover --preflight", docs)
        self.assertIn("asrsub-runtime --reconcile", docs)
        production_section = docs.split("## Deploy", 1)[1].split("## Reverse proxy", 1)[0]
        self.assertNotRegex(production_section, r"ASRSUB_IMAGE=ghcr\.io/bedasrv/asrsub:<full-40-char-sha>")


class TestProductionStateFs(AdapterTestCase):
    def test_statefs_rejects_traversal_and_symlink_and_emits_real_identity(self):
        state = self.root / "state"
        evidence = self.root / "evidence"
        result = run_tool(
            "provision_statefs.py",
            "--test-seam",
            "--state-root",
            state,
            "--evidence-root",
            evidence,
            "--implementation-commit",
            SHA,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt_path = evidence / "statefs-provision" / "statefs-provision-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["implementation_commit"], SHA)
        identity = receipt["root_identity"]
        self.assertGreater(identity["device"], 0)
        self.assertGreater(identity["inode"], 0)
        self.assertGreater(identity["mount_id"], 0)
        self.assertNotEqual(identity["filesystem"], "")
        self.assertTrue((state / "deployment-admission" / "admission.json").is_file())
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((state / "state.jsonl").stat().st_mode), 0o600)

        traversal = run_tool(
            "provision_statefs.py",
            "--production",
            "--state-root",
            self.root / "a" / ".." / "escaped",
            "--evidence-root",
            self.root / "evidence-2",
            "--implementation-commit",
            SHA,
        )
        self.assertNotEqual(traversal.returncode, 0)

        outside = self.root / "outside"
        link = self.root / "state-link"
        link.symlink_to(outside, target_is_directory=True)
        symlink_result = run_tool(
            "provision_statefs.py",
            "--production",
            "--state-root",
            link,
            "--evidence-root",
            self.root / "evidence-3",
            "--implementation-commit",
            SHA,
        )
        self.assertNotEqual(symlink_result.returncode, 0)
        self.assertFalse(outside.exists())

    def test_statefs_production_rejects_fixture_mixing_and_bad_commit(self):
        fixture = self.root / "fixture"
        result = run_tool(
            "provision_statefs.py",
            "--production",
            "--fixture-root",
            fixture,
            "--evidence-root",
            self.root / "evidence",
            "--implementation-commit",
            SHA,
        )
        self.assertNotEqual(result.returncode, 0)
        bad_commit = run_tool(
            "provision_statefs.py",
            "--production",
            "--state-root",
            self.root / "state",
            "--evidence-root",
            self.root / "evidence-2",
            "--implementation-commit",
            "short",
        )
        self.assertNotEqual(bad_commit.returncode, 0)


    def test_statefs_rerun_preserves_valid_admission_and_rejects_malformed_state(self):
        state = self.root / "state"
        first_evidence = self.root / "evidence-1"
        args = [
            "provision_statefs.py",
            "--test-seam",
            "--state-root",
            state,
            "--evidence-root",
            first_evidence,
            "--implementation-commit",
            SHA,
        ]
        first = run_tool(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        admission = state / "deployment-admission" / "admission.json"
        before = admission.read_bytes()
        second_args = [
            "provision_statefs.py",
            "--test-seam",
            "--state-root",
            state,
            "--evidence-root",
            self.root / "evidence-2",
            "--implementation-commit",
            SHA,
        ]
        second = run_tool(*second_args)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(admission.read_bytes(), before)
        admission.write_bytes(b"not-json")
        malformed_args = [
            "provision_statefs.py",
            "--test-seam",
            "--state-root",
            state,
            "--evidence-root",
            self.root / "evidence-3",
            "--implementation-commit",
            SHA,
        ]
        malformed = run_tool(*malformed_args)
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("admission", malformed.stderr.lower())

    def test_production_statefs_rejects_caller_selected_root(self):
        result = run_tool(
            "provision_statefs.py",
            "--production",
            "--state-root",
            self.root / "state",
            "--evidence-root",
            self.root / "evidence",
            "--implementation-commit",
            SHA,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/var/lib/asrsub/state", (result.stdout + result.stderr).lower())


class TestProductionBundleInstall(AdapterTestCase):
    def production_install_args(self, bundle, manifest, approval, verifier, target, output):
        return (
            "--test-seam",
            "--bundle-root",
            bundle,
            "--manifest",
            manifest,
            "--approval",
            approval,
            "--verify-command",
            verifier,
            "--release-sha",
            SHA,
            "--target-root",
            target,
            "--output",
            output,
        )

    def test_bundle_installs_atomically_with_hash_manifest_and_dry_run(self):
        bundle, manifest, approval, verifier, manifest_hash = self.make_bundle()
        target = self.root / "installed"
        output = self.root / "install-receipt.json"
        env = os.environ.copy()
        env["ASRSUB_VERIFY_LOG"] = str(self.root / "verify.log")
        result = subprocess.run(
            [
                PYTHON,
                str(ROOT / "tools" / "install_runtime_bundle.py"),
                *map(str, self.production_install_args(bundle, manifest, approval, verifier, target, output)),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target / "bin" / "asrsub").read_bytes(), b"runtime-binary\n")
        self.assertEqual(stat.S_IMODE((target / "bin" / "asrsub").stat().st_mode), 0o755)
        receipt = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(receipt["manifest_sha256"], manifest_hash)
        self.assertTrue((self.root / "verify.log").exists())
        self.assertNotIn("super-secret-verifier-output", output.read_text(encoding="utf-8"))

        dry_target = self.root / "dry-target"
        dry_output = self.root / "dry-receipt.json"
        dry = subprocess.run(
            [
                PYTHON,
                str(ROOT / "tools" / "install_runtime_bundle.py"),
                *map(str, self.production_install_args(bundle, manifest, approval, verifier, dry_target, dry_output)),
                "--dry-run",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(dry_target.exists())
        self.assertTrue(json.loads(dry_output.read_text(encoding="utf-8"))["dry_run"])

        wrong_mode_target = self.root / "wrong-mode-target"
        wrong_mode_target.mkdir(mode=0o700)
        wrong_mode = run_tool(
            "install_runtime_bundle.py",
            *self.production_install_args(bundle, manifest, approval, verifier, wrong_mode_target, self.root / "wrong-mode.json"),
        )
        self.assertNotEqual(wrong_mode.returncode, 0)
        self.assertEqual(stat.S_IMODE(wrong_mode_target.stat().st_mode), 0o700)

    def test_bundle_rejects_hash_traversal_symlink_and_missing_approval_or_verifier(self):
        bundle, manifest, approval, verifier, _ = self.make_bundle()
        target = self.root / "installed"
        output = self.root / "receipt.json"
        base = list(self.production_install_args(bundle, manifest, approval, verifier, target, output))
        missing_approval = base.copy()
        missing_approval[missing_approval.index("--approval") + 1] = self.root / "missing-approval.json"
        result = run_tool("install_runtime_bundle.py", *missing_approval)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())
        missing_verifier = base.copy()
        missing_verifier[missing_verifier.index("--verify-command") + 1] = self.root / "missing-verifier"
        result = run_tool("install_runtime_bundle.py", *missing_verifier)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())

        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_value["members"][0]["sha256"] = "0" * 64
        manifest.write_bytes(canonical(manifest_value) + b"\n")
        result = run_tool("install_runtime_bundle.py", *base)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())

        bundle, manifest, approval, verifier, _ = self.make_bundle()
        (bundle / "outside-link").symlink_to(self.root / "outside")
        result = run_tool("install_runtime_bundle.py", *self.production_install_args(bundle, manifest, approval, verifier, target, output))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())

        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_value["members"][0]["path"] = "../outside"
        manifest.write_bytes(canonical(manifest_value) + b"\n")
        result = run_tool("install_runtime_bundle.py", *self.production_install_args(bundle, manifest, approval, verifier, target, output))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "outside").exists())

    def test_production_rejects_arbitrary_verifier_even_when_executable(self):
        bundle, manifest, approval, _, _ = self.make_bundle()
        result = run_tool(
            "install_runtime_bundle.py",
            "--production",
            "--bundle-root",
            bundle,
            "--manifest",
            manifest,
            "--approval",
            approval,
            "--verify-command",
            "/usr/bin/true",
            "--release-sha",
            SHA,
            "--target-root",
            self.root / "installed",
            "--output",
            self.root / "receipt.json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixed", (result.stdout + result.stderr).lower())
        self.assertFalse((self.root / "installed").exists())

    def test_test_seam_installs_signed_systemd_members_separately(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            installer = __import__("install_runtime_bundle")
        finally:
            sys.path.pop(0)
        bundle = self.root / "full-bundle"
        bundle.mkdir()
        manifest_members = []
        for name in sorted(installer.EXPECTED_INSTALLED_RUNTIME_MEMBERS):
            path = bundle / name
            path.write_bytes(("runtime-member:" + name + "\n").encode())
            mode = installer.RUNTIME_MEMBER_MODES[name]
            path.chmod(mode)
            manifest_members.append(
                {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": f"{mode:04o}"}
            )
        for name in sorted(installer.EXPECTED_SYSTEMD_MEMBERS):
            path = bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("systemd-member:" + name + "\n").encode())
            mode = installer.SYSTEMD_MEMBER_MODES[name]
            path.chmod(mode)
            manifest_members.append(
                {
                    "path": name,
                    "install_root": "systemd",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "mode": f"{mode:04o}",
                }
            )
        manifest = {"schema": "runtime-bundle-manifest-v1", "release_sha": SHA, "members": manifest_members}
        manifest_path = bundle / "manifest.json"
        manifest_path.write_bytes(canonical(manifest) + b"\n")
        approval = self.root / "approval.json"
        approval.write_text('{"schema":"approval-v1"}\n', encoding="utf-8")
        verifier = self.write_executable("systemd-verifier.py", "#!/usr/bin/env python3\nprint('fixture verifier')\n")
        target = self.root / "installed"
        systemd = self.root / "systemd"
        systemd.mkdir(mode=0o755)
        systemd.chmod(0o755)
        output = self.root / "receipt.json"
        result = run_tool(
            "install_runtime_bundle.py",
            "--test-seam",
            "--bundle-root",
            bundle,
            "--manifest",
            manifest_path,
            "--approval",
            approval,
            "--verify-command",
            verifier,
            "--release-sha",
            SHA,
            "--target-root",
            target,
            "--systemd-root",
            systemd,
            "--output",
            output,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in installer.EXPECTED_INSTALLED_RUNTIME_MEMBERS:
            self.assertTrue((target / name).is_file(), name)
        for name in installer.EXPECTED_SYSTEMD_MEMBERS:
            self.assertTrue((systemd / name.removeprefix("systemd/")).is_file(), name)
        receipt = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            {item["path"] for item in receipt["members"] if item.get("install_root") == "systemd"},
            set(installer.EXPECTED_SYSTEMD_MEMBERS),
        )

    def test_check_fixture_remains_explicit_fixture_mode(self):
        result = run_tool("install_runtime_bundle.py", "--check-fixture")
        self.assertEqual(result.returncode, 0, result.stderr)


class TestProductionRollout(AdapterTestCase):
    def rollout_args(self, inputs, output):
        deployment, bundle, manifest, docker, systemd, cgroup, manifest_hash, _, _ = inputs
        return (
            "--test-seam",
            "--deployment-root",
            deployment,
            "--runtime-bundle",
            bundle,
            "--bundle-manifest",
            manifest,
            "--docker-evidence",
            docker,
            "--systemd-root",
            systemd,
            "--cgroup-root",
            cgroup,
            "--release-sha",
            SHA,
            "--image-digest",
            DIGEST,
            "--bundle-sha256",
            manifest_hash,
            "--output",
            output,
        )

    def test_rollout_receipt_binds_observed_evidence_and_redacts_secrets(self):
        inputs = self.make_rollout_inputs()
        output = self.root / "rollout.json"
        result = run_tool("record_rollout.py", *self.rollout_args(inputs, output))
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(receipt["release_sha"], SHA)
        self.assertEqual(receipt["image_digest"], DIGEST)
        self.assertEqual(receipt["bundle_sha256"], inputs[-3])
        self.assertTrue(receipt["evidence"]["deployment_root"]["observed"])
        self.assertTrue(receipt["evidence"]["cgroup"]["available"])
        self.assertNotIn("secret-from-adapter", output.read_text(encoding="utf-8"))

    def test_rollout_rejects_missing_or_mismatched_evidence(self):
        inputs = self.make_rollout_inputs()
        output = self.root / "rollout.json"
        missing = list(self.rollout_args(inputs, output))
        missing[missing.index("--docker-evidence") + 1] = self.root / "missing-docker.json"
        result = run_tool("record_rollout.py", *missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

        mismatch = list(self.rollout_args(inputs, output))
        mismatch[mismatch.index("--release-sha") + 1] = "2" * 40
        result = run_tool("record_rollout.py", *mismatch)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

        mixed = list(self.rollout_args(inputs, output))
        mixed.extend(["--fixture", self.root / "fixture.json"])
        result = run_tool("record_rollout.py", *mixed)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())


    def test_production_requires_process_health_and_target_artifacts(self):
        inputs = self.make_rollout_inputs()
        args = list(self.rollout_args(inputs, self.root / "rollout.json"))
        args[0] = "--production"
        result = run_tool("record_rollout.py", *args)
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex((result.stdout + result.stderr).lower(), r"health|process|runtime|fixed")
    def test_installed_entrypoint_and_adapters_are_self_contained(self):
        recover = (ROOT / "scripts" / "asrsub-recover").read_text(encoding="utf-8")
        runtime = (ROOT / "scripts" / "asrsub-runtime").read_text(encoding="utf-8")
        entrypoint = (ROOT / "tools" / "production_entrypoint.py").read_text(encoding="utf-8")
        for wrapper, action in ((recover, "recover"), (runtime, "runtime")):
            self.assertIn("/usr/local/libexec/asrsub/production_entrypoint.py", wrapper)
            self.assertNotIn("/opt/mediastack/asrsub/tools", wrapper)
            self.assertIn(action, wrapper)
        self.assertIn("RUNTIME_ROOT / \"compose.yaml\"", entrypoint)
        self.assertNotIn("REPOSITORY_ROOT", entrypoint)
        self.assertNotIn("Path(__file__)", entrypoint)
        for name in ("production_adapter_common.py", "deploy_docker.py"):
            self.assertIn(name, entrypoint)

    def test_production_preflight_requires_signed_manifest_and_install_receipt(self):
        entrypoint = (ROOT / "tools" / "production_entrypoint.py").read_text(encoding="utf-8")
        for needle in (
            "approval.sig",
            "bundle-manifest.sig",
            "runtime-bundle-install.json",
            "_verify_detached",
            "sha256_file",
            "installed runtime",
        ):
            self.assertIn(needle, entrypoint)

    def test_reconcile_pulls_before_inspecting_and_binds_pull_evidence(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            entrypoint = __import__("production_entrypoint")
        finally:
            sys.path.pop(0)
        calls = []
        fake_deploy = types.ModuleType("deploy_docker")

        def fake_run_adapter(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"operation": operation}

        fake_deploy.run_adapter = fake_run_adapter
        approved = {
            "image_digest": DIGEST,
            "release_sha": SHA,
            "compose_sha256": "c" * 64,
        }
        with mock.patch.dict(sys.modules, {"deploy_docker": fake_deploy}), mock.patch.object(
            entrypoint, "preflight", return_value=approved
        ):
            self.assertEqual(entrypoint.reconcile(), 0)
        self.assertEqual([operation for operation, _ in calls[:3]], ["image-pull", "image-inspect", "compose-config"])
        self.assertEqual(calls[3][0], "compose-up")
        self.assertEqual(calls[3][1]["pull_evidence"], entrypoint.PULL_EVIDENCE)

    def test_systemd_and_cgroup_require_expected_entries_but_tolerate_extras(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            rollout = __import__("record_rollout")
        finally:
            sys.path.pop(0)
        systemd = self.root / "systemd"
        (systemd / "docker.service.d").mkdir(parents=True)
        for relative in rollout.EXPECTED_SYSTEMD_FILES:
            path = systemd / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
            path.chmod(0o644)
        (systemd / "unrelated.service").write_text("fixture\n", encoding="utf-8")
        (systemd / "unrelated-link").symlink_to(systemd / "unrelated.service")
        evidence = rollout._systemd_evidence(systemd, production=True)
        self.assertTrue(evidence["observed"])
        cgroup = self.root / "cgroup"
        cgroup.mkdir()
        for name in rollout.EXPECTED_CGROUP_FILES:
            (cgroup / name).write_text("fixture\n", encoding="utf-8")
        (cgroup / "unrelated").write_text("fixture\n", encoding="utf-8")
        cgroup_evidence = rollout._cgroup_evidence(cgroup, production=True)
        self.assertTrue(cgroup_evidence["available"])

    def test_state_evidence_checks_required_subdirectory_metadata(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            rollout = __import__("record_rollout")
        finally:
            sys.path.pop(0)
        state = self.root / "state"
        state.mkdir(mode=0o700)
        for relative in rollout.EXPECTED_STATE_DIRECTORIES:
            directory = state / relative
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        for relative in rollout.EXPECTED_STATE_FILES:
            path = state / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '{"schema":"admission-v1","mode":"running","generation":1,"active":[],"updated_epoch_ns":1}\n'
                if path.name == "admission.json" else "",
                encoding="utf-8",
            )
            path.chmod(0o600)
        evidence = rollout._state_evidence(state, production=True)
        self.assertTrue(evidence["observed"])
        (state / "discord-notifications").chmod(0o755)
        with self.assertRaises(Exception):
            rollout._state_evidence(state, production=True)

    def test_installed_health_probe_emits_health_evidence_v1(self):
        probe = (ROOT / "scripts" / "asrsub-health-probe").read_text(encoding="utf-8")
        self.assertIn("health-evidence-v1", probe)
        self.assertIn("--output", probe)
        self.assertIn("/ready", probe)

    def test_production_manifest_declares_exact_executable_modes(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            installer = __import__("install_runtime_bundle")
        finally:
            sys.path.pop(0)
        self.assertEqual(installer.RUNTIME_MEMBER_MODES["asrsub"], 0o755)
        for name in installer.RUNTIME_EXECUTABLE_MEMBERS:
            self.assertEqual(installer.RUNTIME_MEMBER_MODES[name], 0o755)
        self.assertEqual(installer.RUNTIME_MEMBER_MODES["media-runtime-dependencies.json"], 0o644)

    def test_fixture_approval_policy_is_explicit_without_a_production_signer(self):
        policy = json.loads((ROOT / "tools" / "signer_argv_policy.json").read_text(encoding="utf-8"))
        command = policy["approval"]["command"]
        self.assertIn("--fixture", command)
        self.assertIn("fixture-only", (ROOT / "tools" / "create_approval.py").read_text(encoding="utf-8"))

    def test_production_docker_rejects_unapproved_digest_before_command(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            deploy = __import__("deploy_docker")
        finally:
            sys.path.pop(0)
        approved = self.root / "approved-image.json"
        approved.write_text(
            json.dumps(
                {
                    "schema": "approved-image-v1",
                    "image_ref": DIGEST,
                    "image_digest": "a" * 64,
                    "release_sha": SHA,
                    "platform": "linux/amd64",
                }
            ),
            encoding="utf-8",
        )
        output = self.root / "image-inspect.json"
        with mock.patch.object(deploy, "APPROVED_IMAGE_PATH", approved), mock.patch.object(
            deploy, "IMAGE_INSPECT_EVIDENCE_PATH", output
        ), mock.patch.object(deploy, "DEPLOY_EVIDENCE_ROOT", self.root):
            with self.assertRaises(Exception):
                deploy.run_adapter(
                    "image-inspect",
                    digest="ghcr.io/bedasrv/asrsub@sha256:" + "b" * 64,
                    compose_file=None,
                    output=output,
                    approved_image=approved,
                    release_sha=SHA,
                )
        self.assertFalse(output.exists())

    def test_unverified_install_receipt_approval_is_rejected(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            entrypoint = __import__("production_entrypoint")
        finally:
            sys.path.pop(0)
        with self.assertRaises(Exception):
            entrypoint._validate_installed_members(
                [],
                {
                    "schema": "runtime-bundle-install-receipt-v1",
                    "dry_run": False,
                    "evidence_eligible": True,
                    "approval": {"verified": False},
                },
            )

    def test_missing_detached_signature_is_a_production_blocker(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            installer = __import__("install_runtime_bundle")
        finally:
            sys.path.pop(0)
        with self.assertRaises(Exception):
            installer._require_fixed_path(
                self.root / "missing.sig",
                self.root / "missing.sig",
                name="bundle signature",
            )

    def test_health_docs_match_digest_and_systemd_contract(self):
        health = (ROOT / "docs" / "HEALTH.md").read_text(encoding="utf-8")
        self.assertIn("@sha256:", health)
        self.assertIn("asrsub-recover --preflight", health)
        self.assertIn("asrsub-runtime --reconcile", health)
        self.assertIn("--pull=never", health)


if __name__ == "__main__":
    unittest.main()
