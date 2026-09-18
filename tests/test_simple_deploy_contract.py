"""Offline contracts for the simple immutable Compose deployment path."""
from __future__ import annotations

import json
import sys
import tempfile
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
        self.assertEqual(base[:2], ["docker", "compose"])
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
            secret = Path(directory) / "control_api_key"
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
            "provider_keys.env",
            "required: false",
            "/home/user/.config/asr-pipeline",
            "/home/user/.cache/asr-pipeline",
            "/var/lib/asrsub/state",
            "/mnt/nas/share/media",
            "/home/user/.config/asr-pipeline/secrets",
            "/run/secrets",
        ):
            self.assertIn(needle, text, needle)
        self.assertNotIn("cgroup", text)
        self.assertNotIn("egress-policy", text)
        self.assertNotIn("/usr/local/libexec", text)
        self.assertNotRegex(text, r"(?m)^secrets:\s*$")
        self.assertNotIn("control_api_key:", text)
        self.assertNotIn("discord_webhook:", text)


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
            "release.json",
            "ghcr.io/bedasrv/asrsub@sha256:<64-lowercase-hex>",
            "docker compose config -q",
            "--no-build --pull=never orchestrator",
            "/health",
            "/ready",
            "automatic rollback",
            "rollback status",
            "/home/user/.config/asr-pipeline/secrets/control_api_key",
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
                (path / "metadata.json").write_text(
                    json.dumps(
                        {
                            "schema": "asrsub-simple-backup-v1",
                            "verified": verified,
                            "created_at": path.name,
                            "previous_image": image,
                        }
                    ),
                    encoding="utf-8",
                )
            self.assertEqual(
                deploy.select_latest_verified_backup([old, newest_unverified, newest_verified]),
                old,
            )


if __name__ == "__main__":
    unittest.main()
