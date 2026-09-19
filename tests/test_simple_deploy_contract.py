"""Offline contracts for the simple immutable Compose deployment path."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "asrsub_deploy.py"
TEMPLATE = ROOT / "deploy" / "compose.simple.yaml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
VALID_IMAGE = "ghcr.io/bedasrv/asrsub@sha256:" + "a" * 64


def load_tool():
    if not TOOL.is_file():
        raise AssertionError("tools/asrsub_deploy.py is not implemented")
    sys.path.insert(0, str(TOOL.parent))
    try:
        import asrsub_deploy
    finally:
        sys.path.pop(0)
    return asrsub_deploy


def load_remote_namespace():
    deploy = load_tool()
    namespace = {}
    exec(deploy.REMOTE_SCRIPT, namespace)
    return namespace


class TestImageReferenceValidation(unittest.TestCase):
    def test_accepts_only_the_exact_ghcr_digest_reference(self):
        deploy = load_tool()
        self.assertEqual(deploy.validate_image_reference(VALID_IMAGE), VALID_IMAGE)

    def test_rejects_tags_and_noncanonical_digest_forms(self):
        deploy = load_tool()
        for value in (
            "ghcr.io/bedasrv/asrsub:latest",
            "ghcr.io/bedasrv/asrsub:" + "a" * 40,
            "ghcr.io/bedasrv/asrsub@sha256:" + "A" * 64,
            "docker.io/bedasrv/asrsub@sha256:" + "a" * 64,
            "ghcr.io/bedasrv/asrsub@sha256:" + "a" * 63,
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    deploy.validate_image_reference(value)


class TestSimpleDeployCommands(unittest.TestCase):
    def test_compose_commands_are_argument_lists_without_destructive_operations(self):
        deploy = load_tool()
        base = deploy.compose_command(
            "/opt/mediastack/asrsub",
            "compose.yaml",
            ".env",
            "config",
            "-q",
        )
        self.assertEqual(base[:4], ["/usr/bin/docker", "--context", "default", "compose"])
        self.assertIn("--project-directory", base)
        self.assertIn("--env-file", base)
        self.assertIn("-f", base)
        for operation in (
            ["config", "-q"],
            ["pull", "orchestrator"],
            ["up", "-d", "--no-build", "--pull=never", "orchestrator"],
            ["ps", "--format", "json"],
        ):
            argv = deploy.compose_command(
                "/opt/mediastack/asrsub",
                "compose.yaml",
                ".env",
                *operation,
            )
            self.assertNotIn("down", argv)
            self.assertNotIn("prune", argv)
            self.assertNotIn("rm", argv)
            self.assertNotIn("systemctl", argv)
        with self.assertRaises(ValueError):
            deploy.compose_command(
                "/opt/mediastack/asrsub",
                "compose.yaml",
                ".env",
                "down",
            )

    def test_remote_execution_uses_one_stdin_python_script(self):
        deploy = load_tool()
        command = deploy.ssh_command("target.example")
        self.assertEqual(command[-3:], ["target.example", "python3", "-"])
        self.assertNotIn("-c", command)
        script = deploy.remote_program("status", {"target": "target.example"})
        self.assertIn("import subprocess", script)
        self.assertIn("PAYLOAD_B64", script)
        self.assertNotIn("shell=True", script)

    def test_fake_ssh_runner_exercises_structured_result_without_network(self):
        deploy = load_tool()
        calls = []

        def fake_runner(command, script, timeout):
            calls.append((list(command), script, timeout))
            return deploy.SSHResult(
                0,
                '{"ok":true,"stdout":"sensitive-field-value","message":"safe"}\n',
                "",
            )

        config = deploy.DeploymentConfig(
            target="target.example",
            expected_hostname="target-host",
        )
        result = deploy.run_remote(config, "status", runner=fake_runner)
        self.assertTrue(result["ok"])
        self.assertNotIn("sensitive-field-value", json.dumps(result))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][-3:], ["target.example", "python3", "-"])
        self.assertGreater(len(calls[0][1]), 100)


class TestOutputAndSecretBoundaries(unittest.TestCase):
    def test_public_result_drops_raw_streams_and_sensitive_fields(self):
        deploy = load_tool()
        value = deploy.public_result(
            {
                "ok": False,
                "message": "safe status",
                "stdout": "sensitive-field-value",
                "stderr": "sensitive-field-value",
                "secret": "sensitive-field-value",
            }
        )
        rendered = json.dumps(value)
        self.assertNotIn("sensitive-field-value", rendered)
        self.assertEqual(value["message"], "safe status")

    def test_default_candidate_env_does_not_read_secret_files(self):
        deploy = load_tool()
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "unrelated_secret_sentinel"
            secret.write_text("placeholder-only", encoding="utf-8")
            with mock.patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("secret contents must not be read"),
            ):
                env_text = deploy.build_candidate_env(
                    None,
                    image=VALID_IMAGE,
                    webhook_port="8085",
                    nas_media_prefix="/mnt/nas/share/media",
                )
            secret_source = Path(directory) / "secrets.env"
            secret_source.write_text("NOT_READ=placeholder-only\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                deploy.build_candidate_env(
                    secret_source,
                    image=VALID_IMAGE,
                    webhook_port="8085",
                    nas_media_prefix="/mnt/nas/share/media",
                )
        self.assertIn("ASRSUB_IMAGE=" + VALID_IMAGE, env_text)
        self.assertNotIn("placeholder-only", env_text)

    def test_env_source_rejects_webhook_secret_keys(self):
        deploy = load_tool()
        with tempfile.TemporaryDirectory() as directory:
            for key in ("DISCORD_WEBHOOK_URL", "WEBHOOK_URL"):
                source = Path(directory) / f"{key}.env"
                source.write_text(f"{key}=placeholder-only\n", encoding="utf-8")
                with self.subTest(key=key), self.assertRaisesRegex(
                    ValueError, "secret-bearing env key is not accepted"
                ):
                    deploy.build_candidate_env(
                        source,
                        image=VALID_IMAGE,
                        webhook_port="8085",
                        nas_media_prefix="/mnt/nas/share/media",
                    )

    def test_env_source_retains_explicitly_managed_non_secret_keys(self):
        deploy = load_tool()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "managed.env"
            source.write_text(
                "ASRSUB_IMAGE=ignored\n"
                "WEBHOOK_PORT=ignored\n"
                "NAS_MEDIA_PREFIX=ignored\n"
                "METRICS_URL=https://example.invalid/health\n",
                encoding="utf-8",
            )
            env_text = deploy.build_candidate_env(
                source,
                image=VALID_IMAGE,
                webhook_port="8085",
                nas_media_prefix="/mnt/nas/share/media",
            )

        self.assertEqual(
            env_text,
            "\n".join(
                (
                    "METRICS_URL=https://example.invalid/health",
                    f"ASRSUB_IMAGE={VALID_IMAGE}",
                    "WEBHOOK_PORT=8085",
                    "NAS_MEDIA_PREFIX=/mnt/nas/share/media",
                    "",
                )
            ),
        )


class TestDeployPayloadContract(unittest.TestCase):
    def test_payload_fields_decode_to_the_original_compose_and_env_bytes(self):
        deploy = load_tool()
        config = deploy.DeploymentConfig(
            target="target.example",
            expected_hostname="target-host",
        )
        payload = deploy._payload_for_deploy(config, VALID_IMAGE, TEMPLATE, None)
        self.assertEqual(base64.b64decode(payload["compose_text"]), TEMPLATE.read_bytes())
        expected_env = deploy.build_candidate_env(
            None,
            image=VALID_IMAGE,
            webhook_port=config.webhook_port,
            nas_media_prefix=config.nas_media_prefix,
        ).encode("utf-8")
        self.assertEqual(base64.b64decode(payload["env_text"]), expected_env)
        self.assertNotIn("placeholder-only", base64.b64decode(payload["env_text"]).decode("utf-8"))


class TestPortOwnership(unittest.TestCase):
    def test_parser_proves_the_intended_asrsub_listener_without_raw_ss_output(self):
        deploy = load_tool()
        output = (
            'LISTEN 0 4096 0.0.0.0:8085 0.0.0.0:* '
            'users:(("asrsub",pid=1234,fd=7))\n'
        )
        result = deploy.parse_port_ownership(output, "8085")
        self.assertEqual(result["status"], "owned")
        self.assertEqual(result["process_name"], "asrsub")
        self.assertEqual(result["port"], "8085")
        self.assertNotIn("pid", json.dumps(result))
        self.assertNotIn("raw", result)

    def test_parser_marks_an_unrelated_listener_as_not_owned(self):
        deploy = load_tool()
        output = (
            'LISTEN 0 4096 127.0.0.1:8085 0.0.0.0:* '
            'users:(("python",pid=99,fd=3))\n'
        )
        result = deploy.parse_port_ownership(output, "8085")
        self.assertEqual(result["status"], "unrelated")
        self.assertEqual(result["process_name"], "python")

    def test_parser_requires_the_active_container_pid_not_only_the_process_name(self):
        output = (
            'LISTEN 0 4096 0.0.0.0:8085 0.0.0.0:* '
            'users:(("asrsub",pid=1234,fd=7))\n'
        )
        for parser in (load_tool().parse_port_ownership, load_remote_namespace()["parse_port_ownership"]):
            with self.subTest(parser=parser):
                self.assertEqual(parser(output, "8085", expected_pid=1234)["status"], "owned")
                self.assertEqual(parser(output, "8085", expected_pid=9999)["status"], "unrelated")


class TestMediaMountSafety(unittest.TestCase):
    def test_media_mount_accepts_an_nfs_parent_but_rejects_root_and_local_filesystems(self):
        remote = load_remote_namespace()
        accepted = remote["parse_media_mount"](
            "/mnt/nas/share nfs4 nas.example:/exports/media\n",
            "/mnt/nas/share/media",
        )
        self.assertEqual(accepted["target"], "/mnt/nas/share")
        self.assertEqual(accepted["fstype"], "nfs4")
        with self.assertRaises(remote["RemoteFailure"]):
            remote["parse_media_mount"](
                "/ ext4 /dev/root\n",
                "/mnt/nas/share/media",
            )
        with self.assertRaises(remote["RemoteFailure"]):
            remote["parse_media_mount"](
                "/mnt/nas/share ext4 /dev/sdb1\n",
                "/mnt/nas/share/media",
            )


class TestComposeTemplateContract(unittest.TestCase):
    def test_simple_template_contains_only_the_approved_runtime_shape(self):
        self.assertTrue(TEMPLATE.is_file(), "simple Compose template is missing")
        text = TEMPLATE.read_text(encoding="utf-8")
        for needle in (
            "orchestrator:",
            "${ASRSUB_IMAGE:?",
            "network_mode: host",
            "restart: unless-stopped",
            "WEBHOOK_PORT",
            "NAS_MEDIA_PREFIX",
            "path: ./secrets/provider_keys.env",
            "provider_keys.env",
            "required: false",
            "source: ./config",
            "source: ./cache",
            "source: ./state",
            "source: ./secrets",
            "/home/user/.config/asr-pipeline",
            "/home/user/.cache/asr-pipeline",
            "/mnt/nas/share/media",
            "/run/secrets",
            "/var/lib/asrsub/state",
            "/run/secrets/discord_webhook",
        ):
            self.assertIn(needle, text, needle)
        self.assertNotIn("cgroup", text)
        self.assertNotIn("egress-policy", text)
        self.assertNotIn("/usr/local/libexec", text)
        self.assertNotIn("/var/lib/asrsub/config", text)
        self.assertNotIn("/var/lib/asrsub/cache", text)
        self.assertNotIn("/var/lib/asrsub/runtime-secrets", text)
        self.assertNotIn("control_api_key", text)
        self.assertNotRegex(text, r"(?m)^secrets:\s*$")
        self.assertEqual(text.count("/run/secrets/"), 1)
        self.assertNotIn("discord_webhook:", text)

    def test_config_mount_keeps_application_ledgers_and_statefs_stays_separate(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("target: /home/user/.config/asr-pipeline", text)
        self.assertIn("source: ./state", text)
        self.assertIn("target: /var/lib/asrsub/state", text)
        self.assertNotIn("ASRSUB_CONFIG_DIR:", text)
        self.assertNotIn("STATE_FILE:", text)

    def test_provider_file_is_fixed_to_the_project_relative_optional_env_file(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("env_file:", text)
        self.assertIn("path: ./secrets/provider_keys.env", text)
        self.assertIn("required: false", text)
        self.assertNotIn("PROVIDER_KEYS_FILE", text)


class TestReleaseDescriptorDigestContract(unittest.TestCase):
    def test_release_descriptor_uses_build_push_digest_and_keeps_sha_lookup_tag(self):
        text = RELEASE.read_text(encoding="utf-8")
        self.assertIn("id: build", text)
        self.assertIn("steps.build.outputs.digest", text)
        self.assertIn("ghcr.io/bedasrv/asrsub@${{ steps.build.outputs.digest }}", text)
        self.assertIn("ghcr.io/bedasrv/asrsub:${{ github.sha }}", text)
        deploy = TOOL.read_text(encoding="utf-8") if TOOL.exists() else ""
        self.assertNotIn("steps.build.outputs.digest", deploy)
        self.assertNotIn("github.sha", deploy)


class TestDeployDocumentationContract(unittest.TestCase):
    def test_deploy_runbook_is_self_contained_and_names_safety_boundaries(self):
        text = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
        required = (
            "tools/asrsub_deploy.py",
            "--target <target-host>",
            "--expected-hostname <expected-hostname>",
            "/opt/mediastack/asrsub",
            "compose.yaml",
            "orchestrator",
            "preflight",
            "status",
            "deploy",
            "rollback",
            "legacy Compose shape",
            "pre-apply safety checks",
            "post-apply candidate mount verification",
            "candidate_mounts_verified",
            "saved previous mount contract",
            "legacy/tag",
            "current pipeline ledgers",
            "notification StateFs",
            "/var/lib/asrsub/state",
            "All ASRSub-owned host data",
            "./secrets/provider_keys.env",
            "symlinked parents/targets",
            "never writes or migrates legacy data",
            "release.json",
            "ghcr.io/bedasrv/asrsub@sha256:<64-lowercase-hex>",
            "docker compose config -q",
            "--no-build --pull=never orchestrator",
            "/health",
            "/ready",
            "automatic rollback",
            "rollback status",
            "Pomerium/Pocket ID",
            "chmod 600",
            "never print or read secret values",
            "docker compose down",
            "systemctl restart docker",
            "systemd restart",
            "prune",
            "mutable tag",
            "rm -rf",
            "systemd",
            "drop-in",
            "/usr/local/libexec/asrsub",
            "Troubleshooting",
            "Agent runbook",
        )
        for needle in required:
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_copy_paste_operator_commands_are_present(self):
        text = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
        for operation in ("status", "preflight", "deploy", "rollback"):
            self.assertIn(
                f"./tools/asrsub_deploy.py {operation}",
                text,
                operation,
            )
        self.assertIn("curl --fail --silent", text)


class TestDeployPhases(unittest.TestCase):
    def test_strict_preapply_accepts_known_hardened_legacy_mounts_and_defers_candidate_verification(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            for name in ("config", "cache", "state", "secrets"):
                (project / name).mkdir(mode=0o700)
            (project / "compose.yaml").write_text("legacy", encoding="utf-8")
            (project / ".env").write_text("WEBHOOK_PORT=8085\n", encoding="utf-8")
            payload = {
                "project_directory": str(project),
                "expected_hostname": remote["socket"].gethostname(),
                "compose_file": "compose.yaml",
                "env_file": ".env",
                "service": "orchestrator",
                "project_name": "asrsub",
                "webhook_port": "8085",
                "nas_media_prefix": "/mnt/nas/share/media",
            }
            identity = {
                "labels": {
                    "com.docker.compose.project": "asrsub",
                    "com.docker.compose.service": "orchestrator",
                },
                "mounts": [
                    {
                        "Source": "/var/lib/asrsub/config",
                        "Destination": "/home/user/.config/asr-pipeline",
                        "RW": True,
                    },
                    {
                        "Source": "/var/lib/asrsub/cache",
                        "Destination": "/home/user/.cache/asr-pipeline",
                        "RW": True,
                    },
                    {
                        "Source": "/var/lib/asrsub/state",
                        "Destination": "/var/lib/asrsub/state",
                        "RW": True,
                    },
                    {
                        "Source": "/mnt/nas/share/media",
                        "Destination": "/mnt/nas/share/media",
                        "RW": True,
                    },
                    {
                        "Source": "/mnt/nas/share/media",
                        "Destination": "/media",
                        "RW": False,
                    },
                    {
                        "Source": "/var/lib/asrsub/runtime-secrets/discord_webhook",
                        "Destination": "/run/secrets/discord_webhook",
                        "RW": False,
                    },
                    {
                        "Source": "/sys/fs/cgroup/system.slice/asrsub-runtime.service/asrsub-children",
                        "Destination": "/run/asrsub/children-cgroup",
                        "RW": True,
                    },
                    {
                        "Source": "/usr/local/libexec/asrsub/asrsub",
                        "Destination": "/usr/local/bin/asrsub",
                        "RW": False,
                    },
                    {
                        "Source": "/var/lib/asrsub/egress-policy/egress-policy.json",
                        "Destination": "/run/asrsub/egress-policy.json",
                        "RW": False,
                    },
                ],
                "health": "healthy",
                "config_image": "ghcr.io/bedasrv/asrsub:legacy",
                "image_id": "image-id",
                "repo_digests": [VALID_IMAGE],
            }
            original = {name: remote[name] for name in ("metadata", "require_path", "checked", "active_container", "inspect_container")}
            remote["metadata"] = lambda path: {"present": True, "regular": True, "directory": True, "symlink": False, "mode": 0o600}
            remote["require_path"] = lambda path, **kwargs: {"present": True, "regular": True, "directory": True, "mode": 0o700}
            remote["checked"] = lambda argv, label, **kwargs: (
                'LISTEN 0 4096 0.0.0.0:8085 0.0.0.0:* users:(("asrsub",pid=1,fd=1))\n'
                if argv[0] == "ss"
                else "/mnt/nas/share nfs4 nas.example:/exports/media\n"
                if argv[0] == "findmnt"
                else "ok\n"
            )
            remote["active_container"] = lambda value: ("container-id", {})
            remote["inspect_container"] = lambda value: identity
            try:
                result = remote["collect"](payload, strict=True)
            finally:
                remote.update(original)
            self.assertTrue(result["ok"])
            self.assertEqual(result["checks"]["current_mount_contract"], "legacy-compatible")
            self.assertIsNone(result["checks"]["candidate_mounts_verified"])
            self.assertEqual(result["previous_immutable_repo_digest"], VALID_IMAGE)


class TestRemoteFilesystemSafety(unittest.TestCase):
    def test_atomic_bytes_rejects_a_broken_symlink(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "target"
            os.symlink("missing-target", path)
            with self.assertRaises(remote["RemoteFailure"]):
                remote["atomic_bytes"](path, b"replacement", 0o600)
            self.assertTrue(path.is_symlink())

    def test_create_backup_rejects_symlink_and_non_directory_roots(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            (project / "compose.yaml").write_text("legacy", encoding="utf-8")
            (project / ".env").write_text("WEBHOOK_PORT=8085\n", encoding="utf-8")
            payload = {
                "project_directory": str(project),
                "compose_file": "compose.yaml",
                "env_file": ".env",
            }
            preflight = {"active_image": VALID_IMAGE, "image_id": "image-id", "port": "8085"}
            rollback_root = project / ".asrsub-rollback"
            os.symlink("missing-root", rollback_root)
            with self.assertRaises(remote["RemoteFailure"]):
                remote["create_backup"](payload, preflight)
            rollback_root.unlink()
            rollback_root.write_text("not a directory", encoding="utf-8")
            with self.assertRaises(remote["RemoteFailure"]):
                remote["create_backup"](payload, preflight)

    def test_managed_paths_reject_symlinked_parents_and_unsafe_modes_or_owners(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            project = real / "project"
            project.mkdir(mode=0o700)
            link = root / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(remote["RemoteFailure"]):
                remote["project_paths"](
                    {
                        "project_directory": str(link / "project"),
                        "compose_file": "compose.yaml",
                        "env_file": ".env",
                    }
                )

            compose = project / "compose.yaml"
            compose.write_text("services: {}\n", encoding="utf-8")
            env = project / ".env"
            env.write_text("WEBHOOK_PORT=8085\n", encoding="utf-8")
            project.chmod(0o777)
            with self.assertRaises(remote["RemoteFailure"]):
                remote["require_path"](project, directory=True, role="project")
            project.chmod(0o700)
            compose.chmod(0o666)
            with self.assertRaises(remote["RemoteFailure"]):
                remote["require_path"](compose, directory=False, role="project_file")
            compose.chmod(0o600)

            rollback = project / ".asrsub-rollback"
            rollback.mkdir(mode=0o755)
            with self.assertRaises(remote["RemoteFailure"]):
                remote["require_path"](rollback, directory=True, role="rollback")
            rollback.chmod(0o700)

            original_metadata = remote["metadata"]
            remote["metadata"] = lambda path: {
                "present": True,
                "symlink": False,
                "regular": True,
                "directory": False,
                "mode": 0o600,
                "uid": os.geteuid() + 1,
                "gid": os.getegid(),
            }
            try:
                with self.assertRaises(remote["RemoteFailure"]):
                    remote["require_path"](compose, directory=False, role="project_file")
            finally:
                remote["metadata"] = original_metadata

    def test_atomic_bytes_rejects_a_symlinked_parent_component(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            link = root / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(remote["RemoteFailure"]):
                remote["atomic_bytes"](link / "target", b"replacement", 0o600)
            self.assertFalse((real / "target").exists())


class TestRollbackContracts(unittest.TestCase):
    def test_backup_records_sanitized_legacy_mount_contract(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            (project / "compose.yaml").write_text("legacy", encoding="utf-8")
            (project / ".env").write_text("WEBHOOK_PORT=8085\n", encoding="utf-8")
            payload = {
                "project_directory": str(project),
                "compose_file": "compose.yaml",
                "env_file": ".env",
                "webhook_port": "8085",
                "nas_media_prefix": "/mnt/nas/share/media",
            }
            mounts = [
                {
                    "Source": "/var/lib/asrsub/config",
                    "Destination": "/home/user/.config/asr-pipeline",
                    "RW": True,
                    "Mode": "rw",
                },
            ]
            preflight = {
                "active_image": VALID_IMAGE,
                "image_id": "image-id",
                "port": "8085",
                "_identity": {
                    "config_image": "ghcr.io/bedasrv/asrsub:legacy",
                    "repo_digests": [VALID_IMAGE],
                    "mounts": mounts,
                },
            }
            _backup, record = remote["create_backup"](payload, preflight)
            self.assertEqual(record["backup_kind"], "legacy")
            self.assertEqual(record["previous_repo_digest"], VALID_IMAGE)
            self.assertEqual(
                record["previous_mount_contract"],
                [
                    {
                        "source": "/var/lib/asrsub/config",
                        "destination": "/home/user/.config/asr-pipeline",
                        "rw": True,
                    },
                ],
            )
            self.assertEqual(
                set(record["previous_mount_contract"][0]),
                {"source", "destination", "rw"},
            )

    def test_legacy_rollback_accepts_tag_config_when_repo_digest_matches(self):
        remote = load_remote_namespace()
        contract = [
            {
                "source": "/var/lib/asrsub/config",
                "destination": "/home/user/.config/asr-pipeline",
                "rw": True,
            }
        ]
        record = {
            "backup_kind": "legacy",
            "previous_image": VALID_IMAGE,
            "previous_repo_digest": VALID_IMAGE,
            "previous_mount_contract": contract,
        }
        payload = {"service": "orchestrator", "project_name": "asrsub"}
        identity = {
            "config_image": "ghcr.io/bedasrv/asrsub:legacy",
            "repo_digests": [VALID_IMAGE],
            "health": "healthy",
            "image_id": "image-id",
            "mounts": [
                {
                    "Source": "/var/lib/asrsub/config",
                    "Destination": "/home/user/.config/asr-pipeline",
                    "RW": True,
                }
            ],
        }
        original = {name: remote[name] for name in ("active_container", "inspect_container")}
        remote["active_container"] = lambda value: ("container-id", {})
        remote["inspect_container"] = lambda value: identity
        try:
            result = remote["verify_rollback_runtime"](payload, record, "8085")
        finally:
            remote.update(original)
        self.assertEqual(result["config_reference"], "legacy/tag")
        self.assertTrue(result["repo_digest_matched"])
        self.assertTrue(result["mounts_verified"])

    def test_simple_rollback_requires_exact_digest_config_reference(self):
        remote = load_remote_namespace()
        record = {
            "backup_kind": "simple",
            "previous_image": VALID_IMAGE,
            "previous_repo_digest": VALID_IMAGE,
            "previous_mount_contract": [
                {
                    "source": "/var/lib/asrsub/config",
                    "destination": "/home/user/.config/asr-pipeline",
                    "rw": True,
                }
            ],
        }
        payload = {"service": "orchestrator", "project_name": "asrsub"}
        identity = {
            "config_image": "ghcr.io/bedasrv/asrsub:tagged",
            "repo_digests": [VALID_IMAGE],
            "health": "healthy",
            "image_id": "image-id",
            "mounts": [
                {
                    "Source": "/var/lib/asrsub/config",
                    "Destination": "/home/user/.config/asr-pipeline",
                    "RW": True,
                }
            ],
        }
        original = {name: remote[name] for name in ("active_container", "inspect_container")}
        remote["active_container"] = lambda value: ("container-id", {})
        remote["inspect_container"] = lambda value: identity
        try:
            with self.assertRaises(remote["RemoteFailure"]):
                remote["verify_rollback_runtime"](payload, record, "8085")
        finally:
            remote.update(original)


class TestRemoteRollbackSelection(unittest.TestCase):
    def test_selects_latest_verified_legacy_contract_without_docker(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            root = project / ".asrsub-rollback"
            root.mkdir(parents=True, mode=0o700)
            project.chmod(0o700)
            root.chmod(0o700)
            contract = [
                {
                    "source": "/var/lib/asrsub/config",
                    "destination": "/home/user/.config/asr-pipeline",
                    "rw": True,
                }
            ]
            for name, valid in (("20260101T000000Z", True), ("20260102T000000Z", False), ("20260103T000000Z", True)):
                backup = root / name
                backup.mkdir(mode=0o700)
                compose = b"previous compose\n"
                env = b"WEBHOOK_PORT=8085\n"
                (backup / "compose.yaml").write_bytes(compose)
                (backup / ".env").write_bytes(env)
                record = {
                    "schema": "asrsub-simple-backup-v2",
                    "verified": valid,
                    "created_at": name,
                    "backup_kind": "legacy",
                    "previous_image": VALID_IMAGE,
                    "previous_repo_digest": VALID_IMAGE,
                    "previous_mount_contract": contract if valid else [],
                    "compose_file": "compose.yaml",
                    "env_file": ".env",
                    "compose_sha256": hashlib.sha256(compose).hexdigest(),
                    "env_sha256": hashlib.sha256(env).hexdigest(),
                }
                (backup / "metadata.json").write_text(json.dumps(record), encoding="utf-8")
                for filename in ("compose.yaml", ".env", "metadata.json"):
                    (backup / filename).chmod(0o600)
            payload = {
                "project_directory": str(project),
                "compose_file": "compose.yaml",
                "env_file": ".env",
            }
            self.assertEqual(remote["latest_backup"](payload).name, "20260103T000000Z")


class TestRollbackSelection(unittest.TestCase):
    def test_selects_latest_verified_immutable_backup_only(self):
        deploy = load_tool()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "20260101T000000Z"
            newest_unverified = root / "20260103T000000Z"
            newest_verified = root / "20260102T000000Z"
            for path, verified, image in (
                (old, True, VALID_IMAGE),
                (newest_unverified, False, VALID_IMAGE),
                (newest_verified, True, "ghcr.io/bedasrv/asrsub:mutable"),
            ):
                path.mkdir()
                compose = b"previous compose\n"
                env = b"WEBHOOK_PORT=8085\n"
                (path / "compose.yaml").write_bytes(compose)
                (path / ".env").write_bytes(env)
                (path / "metadata.json").write_text(
                    json.dumps(
                        {
                            "schema": "asrsub-simple-backup-v2",
                            "verified": verified,
                            "created_at": path.name,
                            "previous_image": image,
                            "previous_repo_digest": VALID_IMAGE,
                            "backup_kind": "legacy",
                            "previous_mount_contract": [
                                {
                                    "source": "/var/lib/asrsub/config",
                                    "destination": "/home/user/.config/asr-pipeline",
                                    "rw": True,
                                }
                            ],
                            "compose_file": "compose.yaml",
                            "env_file": ".env",
                            "compose_sha256": hashlib.sha256(compose).hexdigest(),
                            "env_sha256": hashlib.sha256(env).hexdigest(),
                        }
                    ),
                    encoding="utf-8",
                )
            self.assertEqual(
                deploy.select_latest_verified_backup([old, newest_unverified, newest_verified]),
                old,
            )


class TestDeploymentHardeningContracts(unittest.TestCase):
    def test_compose_identifiers_reject_option_injection_and_unsafe_names(self):
        deploy = load_tool()
        for value in ("--remove-orphans", "--build", "bad/name", "bad name", "", "."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy.validate_compose_identifier(value, "service")
        with self.assertRaises(ValueError):
            deploy.DeploymentConfig(
                target="target.example",
                expected_hostname="target-host",
                service="--remove-orphans",
            )
        with self.assertRaises(ValueError):
            deploy.DeploymentConfig(
                target="target.example",
                expected_hostname="target-host",
                project_name="--build",
            )

    def test_remote_revalidates_service_and_project_before_building_argv(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            payload = {
                "project_directory": str(project),
                "compose_file": "compose.yaml",
                "env_file": ".env",
                "service": "--remove-orphans",
                "project_name": "asrsub",
            }
            with self.assertRaises(remote["RemoteFailure"]):
                remote["compose_argv"](payload, "up", "-d", payload["service"])
            payload["service"] = "orchestrator"
            payload["project_name"] = "--build"
            with self.assertRaises(remote["RemoteFailure"]):
                remote["compose_argv"](payload, "up", "-d", payload["service"])

    def test_remote_docker_argv_is_pinned_to_default_context(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            payload = {
                "project_directory": str(project),
                "compose_file": "compose.yaml",
                "env_file": ".env",
                "service": "orchestrator",
                "project_name": "asrsub",
            }
            argv = remote["compose_argv"](payload, "config", "-q")
        self.assertEqual(argv[:4], ["/usr/bin/docker", "--context", "default", "compose"])

    def test_remote_subprocess_environment_excludes_interpolation_and_proxy_overrides(self):
        remote = load_remote_namespace()
        captured = {}
        original_run = remote["subprocess"].run

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            captured.update(kwargs)
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        forbidden = {
            "DOCKER_HOST": "tcp://attacker.invalid",
            "DOCKER_CONTEXT": "attacker",
            "COMPOSE_FILE": "/tmp/attacker.yaml",
            "COMPOSE_PROJECT_NAME": "attacker",
            "ASRSUB_IMAGE": "tagged",
            "WEBHOOK_PORT": "1",
            "NAS_MEDIA_PREFIX": "/tmp/media",
            "PROVIDER_KEYS_FILE": "/tmp/keys",
            "HTTP_PROXY": "http://attacker.invalid",
            "HTTPS_PROXY": "http://attacker.invalid",
        }
        try:
            remote["subprocess"].run = fake_run
            with mock.patch.dict(os.environ, forbidden, clear=False):
                remote["checked"](["/usr/bin/docker", "version"], "Docker version")
        finally:
            remote["subprocess"].run = original_run
        self.assertEqual(captured["env"]["PATH"], "/usr/bin:/bin")
        self.assertTrue(set(captured["env"]).issubset({"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES"}))
        self.assertTrue(set(forbidden).isdisjoint(captured["env"]))

    def test_mutating_lock_contends_and_is_not_deleted(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            payload = {"project_directory": str(project), "expected_hostname": remote["socket"].gethostname(), "compose_file": "compose.yaml", "env_file": ".env"}
            lock_path = project / ".asrsub-deploy.lock"
            with remote["mutation_lock"](payload, timeout=0.2):
                self.assertTrue(lock_path.is_file())
                with self.assertRaises(remote["RemoteFailure"]):
                    with remote["mutation_lock"](payload, timeout=0.05):
                        pass
            self.assertTrue(lock_path.is_file())

    def test_pending_marker_blocks_later_deploy_and_selects_referenced_backup(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            payload = {
                "project_directory": str(project),
                "expected_hostname": remote["socket"].gethostname(),
                "compose_file": "compose.yaml",
                "env_file": ".env",
                "service": "orchestrator",
                "project_name": "asrsub",
            }
            backup = self._complete_backup(remote, project, "20260101T000000Z")
            remote["write_pending"](payload, backup, "deploy", candidate_image=VALID_IMAGE)
            self.assertEqual(remote["latest_backup"](payload), backup)
            with self.assertRaises(remote["RecoveryRequired"]):
                remote["ensure_no_pending"](payload)
            self.assertTrue(remote["pending_marker_path"](payload).is_file())

    def test_pending_marker_is_cleared_only_after_explicit_recovery_success(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            payload = {
                "project_directory": str(project),
                "expected_hostname": remote["socket"].gethostname(),
                "compose_file": "compose.yaml",
                "env_file": ".env",
                "service": "orchestrator",
                "project_name": "asrsub",
            }
            backup = self._complete_backup(remote, project, "20260101T000000Z")
            remote["write_pending"](payload, backup, "rollback")
            marker = remote["pending_marker_path"](payload)
            self.assertTrue(marker.is_file())
            remote["clear_pending"](payload)
            self.assertFalse(marker.exists())

    def test_readiness_budget_matches_runbook_and_embedded_remote_program(self):
        remote = load_remote_namespace()
        text = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
        self.assertIn("waits a bounded 60 seconds for both `/health` and `/ready`", text)
        self.assertEqual(remote["READINESS_TIMEOUT"], 60.0)

    def test_rollback_root_and_backup_files_use_role_aware_nofollow_validation(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            payload = {
                "project_directory": str(project),
                "expected_hostname": remote["socket"].gethostname(),
                "compose_file": "compose.yaml",
                "env_file": ".env",
                "service": "orchestrator",
                "project_name": "asrsub",
            }
            backup = self._complete_backup(remote, project, "20260101T000000Z")
            root = project / ".asrsub-rollback"

            root.chmod(0o755)
            with self.assertRaises(remote["RemoteFailure"]):
                remote["backup_root"](payload)
            root.chmod(0o700)

            backup.chmod(0o755)
            with self.assertRaises(remote["RemoteFailure"]):
                remote["read_backup_record"](payload, backup)
            backup.chmod(0o700)

            original_metadata = remote["metadata"]

            def foreign_owner(path):
                info = original_metadata(path)
                if Path(path).name in {
                    root.name,
                    backup.name,
                    "compose.yaml",
                    ".env",
                    "metadata.json",
                }:
                    info["uid"] = os.geteuid() + 1
                return info

            remote["metadata"] = foreign_owner
            try:
                with self.assertRaises(remote["RemoteFailure"]):
                    remote["backup_root"](payload)
                with self.assertRaises(remote["RemoteFailure"]):
                    remote["read_backup_record"](payload, backup)
            finally:
                remote["metadata"] = original_metadata

    def test_timeout_is_finite_positive_bounded_and_remote_budget_is_forwarded(self):
        deploy = load_tool()
        for value in (0, -1, float("nan"), float("inf"), deploy.MAX_TIMEOUT + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy._validate_timeout(value)
        self.assertGreater(deploy.DEFAULT_TIMEOUT, 120.0)
        calls = []

        def fake_runner(command, script, timeout):
            calls.append((command, script, timeout))
            return deploy.SSHResult(0, '{"ok":true}\n', "")

        config = deploy.DeploymentConfig(
            target="target.example",
            expected_hostname="target-host",
            timeout=12.5,
        )
        deploy.run_remote(config, "status", runner=fake_runner)
        self.assertEqual(calls[0][2], config.timeout + deploy.SSH_TRANSPORT_GRACE)
        encoded = calls[0][1].split("PAYLOAD_B64 = ", 1)[1].splitlines()[0]
        payload = json.loads(base64.b64decode(eval(encoded)).decode("utf-8"))
        self.assertEqual(payload["timeout"], config.timeout)

    def test_remote_checked_clamps_subprocess_timeout_to_deadline(self):
        remote = load_remote_namespace()
        captured = {}
        original_run = remote["subprocess"].run

        def fake_run(argv, **kwargs):
            captured.update(kwargs)
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        try:
            remote["subprocess"].run = fake_run
            deadline = time.monotonic() + 0.4
            remote["checked"](["true"], "bounded", timeout=60, deadline=deadline)
        finally:
            remote["subprocess"].run = original_run
        self.assertGreater(captured["timeout"], 0)
        self.assertLessEqual(captured["timeout"], 0.4)

    def test_stream_ssh_timeout_keeps_recovery_required_and_reaps_pipes_without_killing(self):
        deploy = load_tool()
        reaper_called = threading.Event()

        class FakeProcess:
            def __init__(self):
                self.stdin = mock.Mock()
                self.stdout = mock.Mock()
                self.stderr = mock.Mock()
                self.returncode = None
                self.calls = []
                self.killed = False

            def communicate(self, _input=None, timeout=None):
                self.calls.append(timeout)
                if timeout is not None:
                    if len(self.calls) == 1:
                        raise deploy.subprocess.TimeoutExpired(["ssh"], timeout)
                    reaper_called.set()
                    raise deploy.subprocess.TimeoutExpired(["ssh"], timeout)
                raise AssertionError("timed-out SSH reaper must use a finite timeout")

            def kill(self):
                self.killed = True

        process = FakeProcess()
        with mock.patch.object(deploy.subprocess, "Popen", return_value=process):
            with self.assertRaises(deploy.RecoveryRequired):
                deploy.stream_ssh(["ssh"], "script", 0.01)
        self.assertTrue(reaper_called.wait(1.0))
        self.assertFalse(process.killed)
        self.assertEqual(process.calls[0], 0.01)
        self.assertEqual(process.calls[1], deploy.SSH_REAPER_TIMEOUT)
        self.assertLessEqual(process.calls[1], deploy.SSH_TRANSPORT_GRACE)
        process.stdin.close.assert_called_once_with()

    def test_secret_env_denylist_covers_common_credential_names_but_allows_provider_path(self):
        deploy = load_tool()
        forbidden = (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "ENCRYPTION_KEY",
            "AUTHORIZATION",
            "ACCESS_TOKEN",
            "SESSION_COOKIE",
            "TLS_CERTIFICATE",
            "CLIENT_CREDENTIALS",
        )
        with tempfile.TemporaryDirectory() as directory:
            for key in forbidden:
                source = Path(directory) / f"{key}.env"
                source.write_text(f"{key}=placeholder-only\n", encoding="utf-8")
                with self.subTest(key=key), self.assertRaises(ValueError):
                    deploy.build_candidate_env(
                        source,
                        image=VALID_IMAGE,
                        webhook_port="8085",
                        nas_media_prefix="/mnt/nas/share/media",
                    )
            source = Path(directory) / "managed.env"
            source.write_text("METRICS_URL=https://example.invalid/health\n", encoding="utf-8")
            env_text = deploy.build_candidate_env(
                source,
                image=VALID_IMAGE,
                webhook_port="8085",
                nas_media_prefix="/mnt/nas/share/media",
            )
        self.assertNotIn("PROVIDER_KEYS_FILE", env_text)

    def test_env_source_cannot_override_the_project_relative_provider_file(self):
        deploy = load_tool()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "provider-override.env"
            source.write_text("PROVIDER_KEYS_FILE=./outside/provider_keys.env\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "secret-bearing env key"):
                deploy.build_candidate_env(
                    source,
                    image=VALID_IMAGE,
                    webhook_port="8085",
                    nas_media_prefix="/mnt/nas/share/media",
                )

    def test_backup_refuses_to_copy_an_active_env_with_secret_key(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            (project / "compose.yaml").write_text("legacy", encoding="utf-8")
            (project / ".env").write_text("AWS_ACCESS_KEY_ID=placeholder-only\n", encoding="utf-8")
            payload = {
                "project_directory": str(project),
                "compose_file": "compose.yaml",
                "env_file": ".env",
                "webhook_port": "8085",
                "nas_media_prefix": "/mnt/nas/share/media",
            }
            preflight = {
                "active_image": VALID_IMAGE,
                "previous_immutable_repo_digest": VALID_IMAGE,
                "image_id": "image-id",
                "port": "8085",
                "_identity": {
                    "config_image": "ghcr.io/bedasrv/asrsub:legacy",
                    "repo_digests": [VALID_IMAGE],
                    "mounts": [
                        {
                            "Source": "/var/lib/asrsub/config",
                            "Destination": "/home/user/.config/asr-pipeline",
                            "RW": True,
                        }
                    ],
                },
            }
            with self.assertRaises(remote["RemoteFailure"]):
                remote["create_backup"](payload, preflight)

    def test_rollback_selection_skips_newest_incomplete_or_malformed_metadata(self):
        remote = load_remote_namespace()
        deploy = load_tool()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            payload = {
                "project_directory": str(project),
                "compose_file": "compose.yaml",
                "env_file": ".env",
                "service": "orchestrator",
                "project_name": "asrsub",
            }
            older = self._complete_backup(remote, project, "20260101T000000Z")
            root = project / ".asrsub-rollback"
            newest = root / "20260103T000000Z"
            newest.mkdir()
            (newest / "metadata.json").write_text(
                json.dumps(
                    {
                        "schema": "asrsub-simple-backup-v2",
                        "verified": True,
                        "created_at": "20260103T000000Z",
                        "backup_kind": "legacy",
                        "previous_image": VALID_IMAGE,
                        "previous_repo_digest": VALID_IMAGE,
                        "compose_file": "compose.yaml",
                        "env_file": ".env",
                        "compose_sha256": "0" * 64,
                        "env_sha256": "0" * 64,
                        "previous_mount_contract": [
                            {
                                "source": "/var/lib/asrsub/config",
                                "destination": "/home/user/.config/asr-pipeline",
                                "rw": "false",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(remote["latest_backup"](payload), older)
            self.assertEqual(deploy.select_latest_verified_backup([older, newest]), older)

    def test_rollback_metadata_requires_boolean_rw_and_matching_hashes(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            payload = {
                "project_directory": str(project),
                "compose_file": "compose.yaml",
                "env_file": ".env",
                "service": "orchestrator",
                "project_name": "asrsub",
            }
            backup = self._complete_backup(remote, project, "20260101T000000Z")
            metadata_path = backup / "metadata.json"
            record = json.loads(metadata_path.read_text(encoding="utf-8"))
            record["previous_mount_contract"][0]["rw"] = "false"
            metadata_path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(remote["RemoteFailure"]):
                remote["restore_backup"](payload, backup)

    @staticmethod
    def _complete_backup(remote, project, name):
        project.chmod(0o700)
        root = project / ".asrsub-rollback"
        root.mkdir(mode=0o700, exist_ok=True)
        root.chmod(0o700)
        backup = root / name
        backup.mkdir(mode=0o700)
        compose = b"previous compose\n"
        env = b"WEBHOOK_PORT=8085\n"
        (backup / "compose.yaml").write_bytes(compose)
        (backup / ".env").write_bytes(env)
        record = {
            "schema": "asrsub-simple-backup-v2",
            "verified": True,
            "created_at": name,
            "backup_kind": "legacy",
            "previous_image": VALID_IMAGE,
            "previous_repo_digest": VALID_IMAGE,
            "compose_file": "compose.yaml",
            "env_file": ".env",
            "compose_sha256": hashlib.sha256(compose).hexdigest(),
            "env_sha256": hashlib.sha256(env).hexdigest(),
            "previous_mount_contract": [
                {
                    "source": "/var/lib/asrsub/config",
                    "destination": "/home/user/.config/asr-pipeline",
                    "rw": True,
                }
            ],
            "webhook_port": "8085",
        }
        (backup / "metadata.json").write_text(json.dumps(record), encoding="utf-8")
        for filename in ("compose.yaml", ".env", "metadata.json"):
            (backup / filename).chmod(0o600)
        return backup


if __name__ == "__main__":
    unittest.main()
