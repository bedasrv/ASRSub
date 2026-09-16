import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
SHA = "1" * 40
DIGEST = "ghcr.io/bedasrv/asrsub@sha256:" + "a" * 64


class ProductionCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="asrsub-corrections-")
        self.root = Path(self.tmp.name)
        sys.path.insert(0, str(ROOT / "tools"))
        import deploy_docker  # noqa: PLC0415
        import install_runtime_bundle  # noqa: PLC0415
        import production_entrypoint  # noqa: PLC0415
        import record_rollout  # noqa: PLC0415

        self.deploy = deploy_docker
        self.installer = install_runtime_bundle
        self.entrypoint = production_entrypoint
        self.rollout = record_rollout

    def tearDown(self):
        sys.path.pop(0)
        self.tmp.cleanup()

    def test_direct_production_pull_requires_complete_preflight_before_fake_command(self):
        marker = self.root / "executed"
        fake = self.root / "docker"
        fake.write_text(
            "#!/bin/sh\nprintf executed > %s\n" % marker,
            encoding="utf-8",
        )
        fake.chmod(0o755)
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
        approval = self.root / "approval.json"
        approval.write_text(
            json.dumps(
                {
                    "schema": "approval-v1",
                    "release_sha": SHA,
                    "image_digest": "a" * 64,
                    "approved_docker_socket": "default",
                    "approved_state_root": "/var/lib/asrsub/state",
                }
            ),
            encoding="utf-8",
        )
        output = self.root / "image-pull.json"
        with mock.patch.object(self.deploy, "DOCKER", fake), mock.patch.object(
            self.deploy, "APPROVED_IMAGE_PATH", approved
        ), mock.patch.object(self.deploy, "APPROVAL_PATH", approval), mock.patch.object(
            self.deploy, "IMAGE_PULL_EVIDENCE_PATH", output
        ), mock.patch.object(self.deploy, "DEPLOY_EVIDENCE_ROOT", self.root):
            with self.assertRaises(Exception):
                self.deploy.run_adapter(
                    "image-pull",
                    digest=DIGEST,
                    compose_file=None,
                    output=output,
                    executable=fake,
                    approved_image=approved,
                    release_sha=SHA,
                )
        self.assertFalse(marker.exists(), "production preflight must precede Docker execution")
        self.assertFalse(output.exists())

    def _full_test_bundle(self):
        bundle = self.root / "bundle"
        bundle.mkdir()
        members = []
        for name in sorted(self.installer.EXPECTED_INSTALLED_RUNTIME_MEMBERS):
            path = bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("runtime:" + name + "\n").encode())
            mode = self.installer.RUNTIME_MEMBER_MODES[name]
            path.chmod(mode)
            members.append(
                {
                    "path": name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "mode": f"{mode:04o}",
                }
            )
        for name in sorted(self.installer.EXPECTED_SYSTEMD_MEMBERS):
            path = bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("systemd:" + name + "\n").encode())
            mode = self.installer.SYSTEMD_MEMBER_MODES[name]
            path.chmod(mode)
            members.append(
                {
                    "path": name,
                    "install_root": "systemd",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "mode": f"{mode:04o}",
                }
            )
        manifest = {"schema": "runtime-bundle-manifest-v1", "release_sha": SHA, "members": members}
        manifest_path = bundle / "manifest.json"
        manifest_path.write_bytes(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
        approval = self.root / "approval.json"
        approval.write_text('{"schema":"approval-v1"}\n', encoding="utf-8")
        verifier = self.root / "verifier"
        verifier.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        verifier.chmod(0o755)
        return bundle, manifest_path, approval, verifier

    def test_installer_failure_restores_runtime_and_systemd_without_receipt(self):
        bundle, manifest, approval, verifier = self._full_test_bundle()
        target = self.root / "installed"
        target.mkdir(mode=0o755)
        (target / "old-runtime").write_text("old-runtime\n", encoding="utf-8")
        systemd = self.root / "systemd"
        (systemd / "docker.service.d").mkdir(parents=True)
        old_files = {}
        for relative in ("asrsub-recovery.service", "asrsub-runtime.service", "docker.service.d/asrsub-recovery.conf"):
            path = systemd / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("old-systemd:" + relative + "\n", encoding="utf-8")
            path.chmod(0o644)
            old_files[relative] = path.read_bytes()
        output = self.root / "receipt.json"
        args = argparse.Namespace(
            bundle_root=bundle,
            manifest=manifest,
            approval=approval,
            release_sha=SHA,
            target_root=target,
            output=output,
            systemd_root=systemd,
            verify_command=verifier,
            approval_signature=None,
            approval_public_key=None,
            bundle_signature=None,
            bundle_public_key=None,
            image_digest=None,
            compose_sha256=None,
            compose_template_sha256=None,
            generation=None,
            dry_run=False,
            target_mode=0o755,
        )
        original = self.installer._install_systemd_stage

        def fail_after_runtime(*args, **kwargs):
            raise self.installer.AdapterError("injected systemd failure")

        try:
            with mock.patch.object(self.installer, "_install_systemd_stage", fail_after_runtime):
                with self.assertRaises(Exception):
                    self.installer._production(args, test_seam=True)
        finally:
            self.installer._install_systemd_stage = original
        self.assertEqual((target / "old-runtime").read_bytes(), b"old-runtime\n")
        self.assertEqual(
            {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()},
            {"old-runtime"},
        )
        for relative, contents in old_files.items():
            self.assertEqual((systemd / relative).read_bytes(), contents)
        self.assertFalse(output.exists(), "failed installation must not write a success receipt")

    def test_readiness_wait_uses_successful_probe_only(self):
        evidence = self.root / "health.json"
        attempts = []

        def fake_runner(argv, **kwargs):
            attempts.append((argv, kwargs))
            value = {
                "schema": "health-evidence-v1",
                "endpoint": "/ready",
                "status": 200 if len(attempts) == 2 else None,
                "returncode": 0 if len(attempts) == 2 else 1,
                "ready": len(attempts) == 2,
            }
            evidence.write_text(json.dumps(value) + "\n", encoding="utf-8")
            if value["returncode"]:
                raise self.entrypoint.AdapterError("probe not ready")
            return {"argv": [str(item) for item in argv], "returncode": 0, "stdout": "", "stderr": ""}

        result = self.entrypoint._wait_for_ready(
            probe=self.root / "installed-health-probe",
            output=evidence,
            runner=fake_runner,
            sleep=lambda _: None,
            attempts=3,
        )
        self.assertTrue(result["ready"])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[-1][1]["timeout"], self.entrypoint.HEALTH_PROBE_TIMEOUT)

    def test_cgroup_evidence_rejects_disposable_directory_in_production(self):
        root = self.root / "cgroup"
        root.mkdir()
        (root / "cgroup.controllers").write_text("cpu memory pids\n", encoding="utf-8")
        (root / "cgroup.procs").write_text(str(os.getpid()) + "\n", encoding="utf-8")
        (root / "cgroup.subtree_control").write_text("cpu memory pids\n", encoding="utf-8")
        with self.assertRaises(Exception):
            self.rollout._cgroup_evidence(root, production=True)

    def test_health_probe_connection_failure_is_valid_json_without_network(self):
        curl = self.root / "curl"
        curl.write_text("#!/bin/sh\nprintf 000\nexit 7\n", encoding="utf-8")
        curl.chmod(0o755)
        output = self.root / "health.json"
        result = subprocess.run(
            [
                str(ROOT / "scripts" / "asrsub-health-probe"),
                "--test-seam",
                "--curl-executable",
                str(curl),
                "--output",
                str(output),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        value = json.loads(output.read_text(encoding="utf-8"))
        self.assertIsNone(value["status"])
        self.assertFalse(value["ready"])


    def test_health_probe_success_writes_owned_ready_evidence(self):
        curl = self.root / "curl-success"
        curl.write_text("#!/bin/sh\nprintf 200\nexit 0\n", encoding="utf-8")
        curl.chmod(0o755)
        output = self.root / "health-success.json"
        result = subprocess.run(
            [str(ROOT / "scripts" / "asrsub-health-probe"), "--test-seam", "--curl-executable", str(curl), "--output", str(output)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(value, {"schema": "health-evidence-v1", "endpoint": "/ready", "status": 200, "returncode": 0, "ready": True})
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual((output.stat().st_uid, output.stat().st_gid), (os.getuid(), os.getgid()))

    def test_preflight_metadata_checks_reject_wrong_modes_and_trust_anchors(self):
        uid = os.getuid()
        gid = os.getgid()
        paths = {
            "runtime": self.root / "runtime",
            "state": self.root / "state",
            "deploy": self.root / "deploy",
            "evidence": self.root / "evidence",
            "systemd": self.root / "systemd",
            "trust": self.root / "trust",
            "dropin": self.root / "systemd" / "docker.service.d",
        }
        for name, mode in (("runtime", 0o755), ("state", 0o700), ("deploy", 0o700), ("evidence", 0o700), ("systemd", 0o755), ("trust", 0o700), ("dropin", 0o755)):
            paths[name].mkdir(parents=True, exist_ok=True, mode=mode)
            paths[name].chmod(mode)
        files = {
            "docker": self.root / "docker",
            "openssl": self.root / "openssl",
            "approval": paths["deploy"] / "approval.json",
            "manifest": paths["deploy"] / "bundle-manifest.json",
            "image": paths["deploy"] / "approved-image.json",
            "receipt": paths["evidence"] / "runtime-bundle-install.json",
        }
        for path in files.values():
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o755 if path in {files["docker"], files["openssl"]} else 0o600)
        for name, mode in (("approval.sig", 0o600), ("approval-key.pub", 0o644), ("bundle-manifest.sig", 0o600), ("bundle-signing-key.pub", 0o644)):
            path = paths["trust"] / name
            path.write_text("key\n", encoding="utf-8")
            path.chmod(mode)
        replacements = {
            "RUNTIME_ROOT": paths["runtime"],
            "STATE_ROOT": paths["state"],
            "DEPLOY_STATE_ROOT": paths["deploy"],
            "EVIDENCE_ROOT": paths["evidence"],
            "SYSTEMD_ROOT": paths["systemd"],
            "TRUST_ROOT": paths["trust"],
            "DOCKER": files["docker"],
            "OPENSSL": files["openssl"],
            "APPROVAL": files["approval"],
            "BUNDLE_MANIFEST": files["manifest"],
            "APPROVED_IMAGE": files["image"],
            "INSTALL_RECEIPT": files["receipt"],
            "PRODUCTION_UID": uid,
            "PRODUCTION_GID": gid,
            "SYSTEMD_UID": uid,
            "SYSTEMD_GID": gid,
            "TRUST_UID": uid,
            "TRUST_GID": gid,
        }
        real_ensure_regular_file = self.entrypoint.ensure_regular_file
        def seam_ensure_regular_file(path, *, mode, uid, gid, name):
            return real_ensure_regular_file(path, mode=mode, uid=os.getuid() if uid == 0 else uid, gid=os.getgid() if gid == 0 else gid, name=name)
        replacements["ensure_regular_file"] = mock.Mock(side_effect=seam_ensure_regular_file)
        with mock.patch.multiple(self.entrypoint, **replacements):
            self.entrypoint._validate_host_metadata()
            paths["runtime"].chmod(0o700)
            with self.assertRaises(self.entrypoint.AdapterError):
                self.entrypoint._validate_host_metadata()
            paths["runtime"].chmod(0o755)
            paths["trust"].joinpath("approval.sig").chmod(0o644)
            with self.assertRaises(self.entrypoint.AdapterError):
                self.entrypoint._validate_host_metadata()

    def test_install_receipt_target_identity_is_recomputed_not_self_attested(self):
        runtime = self.root / "runtime"
        systemd = self.root / "systemd"
        runtime.mkdir(mode=0o755)
        systemd.mkdir(mode=0o755)
        member = runtime / "asrsub"
        member.write_text("runtime\n", encoding="utf-8")
        member.chmod(0o755)
        normalized = [
            {
                "path": "asrsub",
                "target": "asrsub",
                "install_root": "runtime",
                "sha256": hashlib.sha256(member.read_bytes()).hexdigest(),
                "mode": 0o755,
            }
        ]
        receipt = {
            "schema": "runtime-bundle-install-receipt-v1",
            "record_authority": "non-authoritative-install-record-v1",
            "dry_run": False,
            "evidence_eligible": True,
            "target_root": str(runtime),
            "systemd_root": str(systemd),
            "target_identity": {"device": 1, "inode": 1, "mount_id": 1, "filesystem": "ext4"},
            "members": normalized,
            "approval": {"verified": True},
        }
        with mock.patch.multiple(
            self.entrypoint,
            RUNTIME_ROOT=runtime,
            SYSTEMD_ROOT=systemd,
            PRODUCTION_UID=os.getuid(),
            PRODUCTION_GID=os.getgid(),
            filesystem_identity=mock.Mock(return_value={"device": 2, "inode": 2, "mount_id": 2, "filesystem": "ext4"}),
        ):
            with self.assertRaises(self.entrypoint.AdapterError):
                self.entrypoint._validate_installed_members(normalized, receipt)

    def test_health_probe_connection_failure_is_valid_json_without_network_via_fake_curl(self):
        curl = self.root / "curl"
        curl.write_text("#!/bin/sh\nprintf 000\nexit 7\n", encoding="utf-8")
        curl.chmod(0o755)
        output = self.root / "health.json"
        result = subprocess.run(
            [str(ROOT / "scripts" / "asrsub-health-probe"), "--test-seam", "--curl-executable", str(curl), "--output", str(output)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(json.loads(output.read_text(encoding="utf-8"))["status"])

    def test_signing_tools_use_real_key_fds_in_test_seams(self):
        key = self.root / "key.pem"
        public = self.root / "key.pub"
        subprocess.run(
            ["/usr/bin/openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["/usr/bin/openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        runtime = self.root / "runtime"
        systemd = self.root / "systemd"
        runtime.mkdir()
        systemd.mkdir()
        for name in self.installer.EXPECTED_INSTALLED_RUNTIME_MEMBERS:
            path = runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("release:" + name + "\n", encoding="utf-8")
            path.chmod(self.installer.RUNTIME_MEMBER_MODES[name])
        for name in self.installer.EXPECTED_SYSTEMD_MEMBERS:
            path = systemd / name.removeprefix("systemd/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("unit:" + name + "\n", encoding="utf-8")
            path.chmod(0o644)
        bundle = self.root / "bundle"
        manifest = self.root / "bundle-manifest.json"
        signature = self.root / "bundle-manifest.sig"
        key_fd = os.open(key, os.O_RDONLY)
        try:
            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "tools" / "package_bundle.py"),
                    "--test-seam",
                    "--runtime-source-root",
                    str(runtime),
                    "--systemd-source-root",
                    str(systemd),
                    "--output-root",
                    str(bundle),
                    "--manifest-output",
                    str(manifest),
                    "--signature-output",
                    str(signature),
                    "--release-sha",
                    SHA,
                    "--key-fd",
                    str(key_fd),
                ],
                cwd=ROOT,
                pass_fds=(key_fd,),
                capture_output=True,
                text=True,
            )
        finally:
            os.close(key_fd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((bundle / "compose.yaml").is_file())
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest_value["schema"], "runtime-bundle-manifest-v1")
        self.assertEqual(
            {item["path"] for item in manifest_value["members"]},
            set(self.installer.EXPECTED_INSTALLED_RUNTIME_MEMBERS) | set(self.installer.EXPECTED_SYSTEMD_MEMBERS),
        )
        verify = subprocess.run(
            ["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(signature), str(manifest)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)
        approval = self.root / "approval-input.json"
        approval.write_bytes(
            json.dumps(
                {
                    "schema": "approval-v1",
                    "generation": 1,
                    "release_sha": SHA,
                    "bundle_sha256": "b" * 64,
                    "image_digest": "a" * 64,
                    "compose_sha256": "c" * 64,
                    "compose_template_sha256": "d" * 64,
                    "notifications_enabled": False,
                    "approved_docker_socket": "default",
                    "approved_state_root": "/var/lib/asrsub/state",
                    "created_epoch_ns": 0,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        approval_out = self.root / "approval.json"
        approval_sig = self.root / "approval.sig"
        key_fd = os.open(key, os.O_RDONLY)
        try:
            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "tools" / "create_approval.py"),
                    "--test-seam",
                    "--canonical-approval-bytes",
                    str(approval),
                    "--approval-manifest",
                    str(approval_out),
                    "--approval-signature",
                    str(approval_sig),
                    "--approval-key-fd",
                    str(key_fd),
                ],
                cwd=ROOT,
                pass_fds=(key_fd,),
                capture_output=True,
                text=True,
            )
        finally:
            os.close(key_fd)
        self.assertEqual(result.returncode, 0, result.stderr)
        verify = subprocess.run(
            ["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(approval_sig), str(approval_out)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)

    def test_production_signing_rejects_missing_release_inputs_and_key(self):
        for tool in ("package_bundle.py", "create_approval.py"):
            result = subprocess.run(
                [PYTHON, str(ROOT / "tools" / tool), "--production"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0, tool)
            self.assertIn("requires", result.stderr.lower(), tool)


if __name__ == "__main__":
    unittest.main()
