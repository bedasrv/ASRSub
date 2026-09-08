"""Immutable release contract — fail-closed, no mutable latest default.

Covers:
- docker-compose.yml must use explicit ${ASRSUB_IMAGE:?} and must NOT contain build: or :latest fallback.
- Secrets and all existing mounts/state must be preserved.
- build.sh must build a full 40-char git-SHA tag and emit a non-secret release descriptor/env.
- deploy.sh must require the explicit release image and use --no-build.
"""
import os
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "docker-compose.yml"
BUILD = REPO / "build.sh"
DEPLOY = REPO / "deploy.sh"
DEPLOY_MD = REPO / "DEPLOY.md"


class TestComposeImmutableRelease(unittest.TestCase):
    def setUp(self):
        self.compose = COMPOSE.read_text(encoding="utf-8")
        self.build = BUILD.read_text(encoding="utf-8")
        self.deploy = DEPLOY.read_text(encoding="utf-8")
        self.deploy_md = DEPLOY_MD.read_text(encoding="utf-8") if DEPLOY_MD.exists() else ""

    def test_compose_has_no_build_directive(self):
        # Permanent correction: compose must NOT build at deploy time.
        self.assertNotRegex(self.compose, r"(?m)^\s*build\s*:", msg="docker-compose.yml must not contain 'build:' (immutable release)")

    def test_compose_image_is_fail_closed_ASRSUB_IMAGE(self):
        # Both services must use ${ASRSUB_IMAGE:? ...} and no default latest.
        # Require at least two occurrences (orchestrator + dashboard).
        pattern = r"\$\{ASRSUB_IMAGE:\?"
        matches = re.findall(pattern, self.compose)
        self.assertGreaterEqual(len(matches), 2, msg="compose must reference ${ASRSUB_IMAGE:?} for both services (fail-closed)")
        # Must NOT contain a fallback default like :- or :?
        # Actually :? is required, but :- would be mutable default — forbid :- for ASRSUB_IMAGE
        self.assertNotIn("ASRSUB_IMAGE:-", self.compose, msg="compose must not use :- default for ASRSUB_IMAGE")
        self.assertNotIn("asrsub:latest", self.compose, msg="compose must not contain mutable 'asrsub:latest' default")

    def test_compose_image_exact_line(self):
        # image: line must be exactly the variable, no hardcoded tag.
        for line in self.compose.splitlines():
            s = line.strip()
            if s.startswith("image:"):
                self.assertRegex(s, r"image:\s*\$\{ASRSUB_IMAGE:\?", msg=f"image line must be fail-closed: {s!r}")

    def test_compose_preserves_secrets(self):
        self.assertIn("control_api_key", self.compose, msg="compose must preserve control_api_key secret")
        self.assertIn("/run/secrets/control_api_key", self.compose, msg="compose must preserve secret mount path")
        self.assertIn("CONTROL_API_KEY_FILE", self.compose, msg="compose must preserve CONTROL_API_KEY_FILE env")
        # secrets block present
        self.assertRegex(self.compose, r"(?m)^secrets:\s*$")
        self.assertIn("CONTROL_API_KEY_FILE_HOST", self.compose)

    def test_compose_preserves_all_mounts_and_state(self):
        # All existing volumes must remain.
        required_mounts = [
            "/home/user/.config/asr-pipeline:/home/user/.config/asr-pipeline",
            "/home/user/.cache/asr-pipeline:/home/user/.cache/asr-pipeline",
            "/home/user/.cache/huggingface:/home/user/.cache/huggingface",
            "/mnt/nas/share/media:/mnt/nas/share/media",
        ]
        for m in required_mounts:
            self.assertIn(m, self.compose, msg=f"compose must preserve mount {m!r}")
        # dashboard read-only config
        self.assertIn("/home/user/.config/asr-pipeline:/home/user/.config/asr-pipeline:ro", self.compose)
        self.assertIn("/mnt/nas/share/media:/mnt/nas/share/media:ro", self.compose)
        # restart policy preserved
        self.assertIn("restart: unless-stopped", self.compose)
        self.assertIn("network_mode: host", self.compose)
        self.assertIn("runtime: nvidia", self.compose)

    def test_compose_preserves_service_commands(self):
        self.assertIn("orchestrator.py", self.compose)
        self.assertIn("dashboard.py", self.compose)


class TestBuildReleaseMetadata(unittest.TestCase):
    def setUp(self):
        self.build = BUILD.read_text(encoding="utf-8")

    def test_build_uses_full_git_sha(self):
        # Must use full 40-char SHA, not --short.
        self.assertIn("git rev-parse HEAD", self.build, msg="build.sh must use 'git rev-parse HEAD' for full SHA")
        self.assertNotIn("rev-parse --short", self.build, msg="build.sh must not use --short (require full 40-char SHA)")
        # Must validate 40 hex chars.
        self.assertRegex(self.build, r"40", msg="build.sh must validate 40-char hex SHA")
        self.assertRegex(self.build, r"\[0-9a-f\]", msg="build.sh must validate hex SHA")

    def test_build_does_not_use_compose_build_with_latest_fallback(self):
        # Must not do 'docker compose build' without tag, nor tag latest as mutable default.
        # Should use 'docker build -t asrsub:${GIT_SHA}' or similar direct build.
        self.assertRegex(self.build, r"docker\s+build", msg="build.sh must use 'docker build' with explicit SHA tag")
        self.assertNotIn("asrsub:latest", self.build, msg="build.sh must not reference mutable asrsub:latest as default")

    def test_build_emits_non_secret_release_descriptor(self):
        # Must emit a descriptor/env file for deploy, containing ASRSUB_IMAGE and GIT_SHA.
        self.assertRegex(self.build, r"\.release\.env|release\.env|release\.json", msg="build.sh must emit a release descriptor/env file")
        self.assertIn("ASRSUB_IMAGE", self.build, msg="build.sh must emit ASRSUB_IMAGE in descriptor")
        # Descriptor must not contain secrets.
        # Ensure build.sh does not write CONTROL_API_KEY into descriptor.
        # Check that descriptor writes are limited to ASRSUB_IMAGE/GIT_SHA etc.
        for secret in ("CONTROL_API_KEY", "BAZARR_API_KEY", "SONARR_API_KEY"):
            # Allow comments mentioning secrets, but not writing them into descriptor.
            # Fail if build.sh writes secret into release file.
            self.assertNotRegex(self.build, rf"release.*{secret}|{secret}.*release", msg=f"build.sh must not emit secret {secret} into release descriptor")

    def test_build_image_tag_format(self):
        self.assertRegex(self.build, r"asrsub:\$\{?GIT_SHA", msg="build.sh must tag image as asrsub:${GIT_SHA}")


class TestDeployRequiresExplicitImage(unittest.TestCase):
    def setUp(self):
        self.deploy = DEPLOY.read_text(encoding="utf-8")

    def test_deploy_requires_ASRSUB_IMAGE_set(self):
        self.assertIn("ASRSUB_IMAGE", self.deploy, msg="deploy.sh must reference ASRSUB_IMAGE")
        # Fail-closed check: aborts if ASRSUB_IMAGE empty/unset.
        self.assertRegex(self.deploy, r"ASRSUB_IMAGE.*:?-|ASRSUB_IMAGE.*\?|if.*ASRSUB_IMAGE|ASRSUB_IMAGE.*must be set|ASRSUB_IMAGE.*required", msg="deploy.sh must fail closed if ASRSUB_IMAGE is not set")

    def test_deploy_uses_no_build(self):
        self.assertIn("--no-build", self.deploy, msg="deploy.sh must use 'docker compose ... --no-build' to avoid rebuilding mutable image")

    def test_deploy_does_not_implicitly_build(self):
        # Must not run bare 'docker compose build' or 'docker compose up' without --no-build.
        # Allow 'docker compose up -d --no-build' only.
        # Check that every 'docker compose up' has --no-build.
        for line in self.deploy.splitlines():
            stripped = line.strip()
            if "docker compose up" in stripped and not stripped.startswith("#"):
                self.assertIn("--no-build", stripped, msg=f"deploy.sh 'up' must include --no-build: {stripped!r}")

    def test_deploy_supports_release_env_file(self):
        # Should load or reference the release env file emitted by build.sh.
        self.assertRegex(self.deploy, r"\.release\.env|release\.env|ASRSUB_IMAGE", msg="deploy.sh must consume the release descriptor/env from build.sh")

    def test_deploy_preserves_secret_and_mount_checks(self):
        self.assertIn("CONTROL_API_KEY_FILE_HOST", self.deploy, msg="deploy.sh must preserve secret file verification")
        self.assertIn("/mnt/nas/share/media", self.deploy, msg="deploy.sh must preserve NFS mount check")


class TestDeployDocsMatch(unittest.TestCase):
    def setUp(self):
        self.md = DEPLOY_MD.read_text(encoding="utf-8") if DEPLOY_MD.exists() else ""

    def test_docs_mention_ASRSUB_IMAGE_and_no_build(self):
        self.assertIn("ASRSUB_IMAGE", self.md, msg="DEPLOY.md must document ASRSUB_IMAGE")
        self.assertIn("--no-build", self.md, msg="DEPLOY.md must document --no-build")

    def test_docs_rollback_uses_previous_sha_not_latest(self):
        # Rollback must use explicit SHA tag, not latest.
        self.assertNotIn("asrsub:latest", self.md, msg="DEPLOY.md rollback must not use asrsub:latest")
        self.assertRegex(self.md, r"asrsub:[0-9a-f]{7,40}|ASRSUB_IMAGE", msg="DEPLOY.md rollback must reference explicit SHA-tagged image")

    def test_docs_describe_release_descriptor(self):
        self.assertRegex(self.md, r"\.release\.env|release\.env|release\.json|release descriptor", msg="DEPLOY.md must describe the release descriptor/env emitted by build.sh")


if __name__ == "__main__":
    unittest.main()
