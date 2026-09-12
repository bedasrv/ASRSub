"""Immutable release contract — fail-closed, no mutable latest default.

Covers:
- docker-compose.yml must use explicit ${ASRSUB_IMAGE:?} and must NOT contain build: or :latest fallback.
- Secrets and all existing mounts/state must be preserved.
- build.sh must build a full 40-char git-SHA tag and emit a non-secret release descriptor/env.
- Release images come from CI into GHCR (full-SHA tags, no latest);
  deploy is pull-based and deploy.sh is deleted.
"""
import os
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "docker-compose.yml"
BUILD = REPO / "build.sh"
DEPLOY = REPO / "deploy.sh"
DEPLOY_MD = REPO / "docs" / "DEPLOY.md"


class TestComposeImmutableRelease(unittest.TestCase):
    def setUp(self):
        self.compose = COMPOSE.read_text(encoding="utf-8")
        self.build = BUILD.read_text(encoding="utf-8")
        self.deploy_md = DEPLOY_MD.read_text(encoding="utf-8") if DEPLOY_MD.exists() else ""

    def test_compose_has_no_build_directive(self):
        # Permanent correction: compose must NOT build at deploy time.
        self.assertNotRegex(self.compose, r"(?m)^\s*build\s*:", msg="docker-compose.yml must not contain 'build:' (immutable release)")

    def test_compose_image_is_fail_closed_ASRSUB_IMAGE(self):
        # Every service must use ${ASRSUB_IMAGE:? ...} and no default latest.
        pattern = r"\$\{ASRSUB_IMAGE:\?"
        matches = re.findall(pattern, self.compose)
        self.assertGreaterEqual(len(matches), 1, msg="compose must reference ${ASRSUB_IMAGE:?} (fail-closed)")
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
        # State, cache and media mounts must remain. The media host path is
        # configurable but defaults to the historical location, and the
        # daemon's container prefix must match the mount target.
        self.assertIn(
            "/home/user/.config/asr-pipeline:/home/user/.config/asr-pipeline",
            self.compose,
        )
        self.assertIn(
            "/home/user/.cache/asr-pipeline:/home/user/.cache/asr-pipeline",
            self.compose,
        )
        # Media mounted at the configurable host prefix ...
        self.assertIn("${NAS_MEDIA_PREFIX:-/mnt/nas/share/media}", self.compose)
        self.assertIn("${MEDIA_HOST_PATH:-/mnt/nas/share/media}", self.compose)
        # ... and at the Jellyfin server path (issue #4).
        self.assertIn("${JELLYFIN_MEDIA_ROOT:-/media}", self.compose)
        # restart policy preserved
        self.assertIn("restart: unless-stopped", self.compose)
        self.assertIn("network_mode: host", self.compose)
        # Rust rewrite is remote-only inference: no local weights, so no
        # huggingface cache mount and no nvidia runtime (regression guard:
        # re-adding either means dragging local inference back in).
        self.assertNotIn("huggingface", self.compose)
        self.assertNotIn("runtime: nvidia", self.compose)

    def test_compose_single_daemon_serves_dashboard(self):
        # Issue #3: no separate read-only dashboard replica (it looped on
        # EROFS). The orchestrator serves `/` and `/api2/*` itself.
        self.assertNotRegex(self.compose, r"(?m)^\s{dashboard}:")
        self.assertNotIn("dashboard.py", self.compose)
        # Read-only state mount is gone with it.
        self.assertNotIn(
            "/home/user/.config/asr-pipeline:/home/user/.config/asr-pipeline:ro",
            self.compose,
        )

    def test_compose_healthcheck_uses_readiness(self):
        # Issue #7: readiness gates the container; liveness stays /health.
        self.assertIn("/ready", self.compose)
        self.assertIn("healthcheck:", self.compose)

    def test_compose_preserves_service_commands(self):
        # Rust binary entrypoint (was: orchestrator.py / dashboard.py).
        self.assertIn("command: [daemon]", self.compose)
        self.assertNotIn("orchestrator.py", self.compose)


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


class TestCICDGHCRContract(unittest.TestCase):
    """Release path is CI-built GHCR images, pull-deployed; deploy.sh is gone."""

    def setUp(self):
        self.release = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.md = DEPLOY_MD.read_text(encoding="utf-8") if DEPLOY_MD.exists() else ""

    def test_deploy_script_removed_and_docs_describe_pull_flow(self):
        self.assertFalse(DEPLOY.exists(), msg="deploy.sh is deleted; deploy is pull-based (see docs/DEPLOY.md)")
        self.assertIn("ghcr.io", self.md, msg="DEPLOY.md must document the GHCR image")
        self.assertIn("docker compose pull", self.md, msg="DEPLOY.md must document pull-based deploy")

    def test_release_workflow_pushes_full_sha_tag_to_ghcr(self):
        self.assertIn("ghcr.io/bedasrv/asrsub", self.release)
        # The full 40-char SHA tag is the authoritative immutable release tag.
        self.assertRegex(self.release, r"ghcr\.io/bedasrv/asrsub:\$\{\{\s*github\.sha\s*\}\}",
                         msg="release must push the full 40-char github.sha tag")
        self.assertIn("packages: write", self.release, msg="release needs packages:write (least privilege)")
        self.assertIn("GITHUB_TOKEN", self.release, msg="GHCR auth via GITHUB_TOKEN, no long-lived PAT")

    def test_release_publishes_latest_as_convenience_alias(self):
        # `latest` must track main so public pulls get the current Rust
        # image (it used to hold the retired Python image). The SHA tag
        # stays the authoritative, immutable deploy target.
        self.assertIn("ghcr.io/bedasrv/asrsub:latest", self.release)

    def test_compose_and_build_never_use_latest(self):
        build = (REPO / "build.sh").read_text(encoding="utf-8")
        compose = COMPOSE.read_text(encoding="utf-8")
        for name, text in (("docker-compose.yml", compose), ("build.sh", build)):
            self.assertNotIn("asrsub:latest", text, msg=f"{name} must not use mutable latest")

    def test_release_triggers_on_main(self):
        self.assertRegex(self.release, r"(?s)on:.*?branches:\s*\[main\]",
                         msg="release must trigger on pushes to main")

    def test_ci_workflow_gates_on_tests(self):
        for needle in ("cargo test", "clippy", "cargo fmt", "unittest"):
            self.assertIn(needle, self.ci, msg=f"ci.yml must run {needle}")


class TestDeployDocsMatch(unittest.TestCase):
    def setUp(self):
        self.md = DEPLOY_MD.read_text(encoding="utf-8") if DEPLOY_MD.exists() else ""

    def test_docs_mention_ASRSUB_IMAGE_and_no_build(self):
        self.assertIn("ASRSUB_IMAGE", self.md, msg="DEPLOY.md must document ASRSUB_IMAGE")
        self.assertIn("--no-build", self.md, msg="DEPLOY.md must document --no-build")

    def test_docs_rollback_uses_previous_sha_not_latest(self):
        # The Rollback section must pin an explicit previous SHA; no deploy
        # command may target the moving latest alias.
        section = self.md.split("## Rollback", 1)[1].split("\n## ", 1)[0] if "## Rollback" in self.md else ""
        self.assertRegex(section, r"asrsub:<previous-40-char-sha>|ASRSUB_IMAGE",
                         msg="rollback must pin an explicit previous SHA")
        for line in section.splitlines():
            if "docker compose" in line or line.strip().startswith("ASRSUB_IMAGE="):
                self.assertNotRegex(line, r"asrsub:latest",
                                    msg=f"rollback must not pin latest: {line!r}")

    def test_docs_describe_release_descriptor(self):
        self.assertRegex(self.md, r"\.release\.env|release\.env|release\.json|release descriptor", msg="DEPLOY.md must describe the release descriptor/env emitted by build.sh")


class TestHealthAndJellyfinContract(unittest.TestCase):
    """Cross-file guards for issues #2/#4/#5/#7/#8."""

    def setUp(self):
        self.compose = COMPOSE.read_text(encoding="utf-8")
        self.config = (REPO / "src" / "config.rs").read_text(encoding="utf-8")
        self.health = (REPO / "docs" / "HEALTH.md").read_text(encoding="utf-8")
        self.deploy_md = DEPLOY_MD.read_text(encoding="utf-8") if DEPLOY_MD.exists() else ""
        self.env_example = (REPO / "pipeline.env.example").read_text(encoding="utf-8")

    def test_no_site_specific_jellyfin_url_default(self):
        # Issue #8: no compiled-in private address in source, docs or example.
        plan_md = (REPO / "docs" / "PLAN.md").read_text(encoding="utf-8")
        for text, name in (
            (self.config, "src/config.rs"),
            (self.deploy_md, "docs/DEPLOY.md"),
            (self.env_example, "pipeline.env.example"),
            (plan_md, "docs/PLAN.md"),
        ):
            self.assertNotIn(
                "10.10.20.160", text, msg=f"{name} must not carry a site-specific default"
            )
        self.assertRegex(self.config, r'DEFAULT_JELLYFIN_URL:\s*&str\s*=\s*""')
        # The example config keeps the required URL visible.
        self.assertIn("JELLYFIN_URL", self.env_example)

    def test_ready_endpoint_documented_and_gated(self):
        # Issue #7: /ready contract + compose healthcheck on /ready.
        self.assertIn("/ready", self.health)
        self.assertIn("/ready", self.compose)

    def test_deploy_smoke_script_present(self):
        # Issue #4: smoke test checks media root + host/Jellyfin mapping.
        smoke = REPO / "scripts" / "deploy_smoke.sh"
        self.assertTrue(smoke.exists(), msg="scripts/deploy_smoke.sh must exist")
        text = smoke.read_text(encoding="utf-8")
        for needle in ("/ready", "NAS_MEDIA_PREFIX", "JELLYFIN_MEDIA_ROOT"):
            self.assertIn(needle, text)

    def test_reverse_proxy_documented(self):
        # Issue #6: port 8085, firewall/ACL, probes, SSO vs upstream.
        self.assertIn("8085", self.deploy_md)
        self.assertIn("firewall", self.deploy_md.lower())
        self.assertIn("/ready", self.deploy_md)

    def test_jellyfin_uses_media_authorization_header(self):
        # Issue #2: every request uses MediaBrowser auth, never X-Emby-Token.
        jellyfin = (REPO / "src" / "jellyfin.rs").read_text(encoding="utf-8")
        self.assertNotIn('X-Emby-Token", &self.key', jellyfin)
        self.assertIn("MediaBrowser Token=", jellyfin)
        # All four protected call sites use the shared helper.
        self.assertEqual(jellyfin.count('"Authorization", self.auth_value()'), 4)


class TestServerRenderedDashboard(unittest.TestCase):
    """The htmx dashboard replaces the retired single-file dashboard.html."""

    def setUp(self):
        self.web = (REPO / "src" / "web.rs").read_text(encoding="utf-8")
        self.dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
        self.assets = REPO / "assets"

    def test_old_dashboard_removed(self):
        self.assertFalse(
            (self.assets / "dashboard.html").exists(),
            msg="assets/dashboard.html is replaced by the server-rendered dashboard",
        )

    def test_htmx_and_css_embedded(self):
        for name in ("htmx.min.js", "app.css"):
            self.assertTrue((self.assets / name).exists(), msg=f"missing asset {name}")
        # Embedded at compile time, not read from disk at runtime.
        self.assertIn("include_str!", self.web)
        self.assertIn("htmx.min.js", self.web)
        self.assertIn("app.css", self.web)

    def test_settings_generated_from_rust_schema(self):
        # The field schema is the single source of truth, shared with the daemon.
        config = (REPO / "src" / "config.rs").read_text(encoding="utf-8")
        self.assertIn("pub const FIELDS", config)
        self.assertIn("pub fn is_editable_key", config)
        self.assertIn("FIELDS", self.web)

    def test_docker_build_stage_copies_assets(self):
        # include_str! needs assets/ present during `cargo build`.
        self.assertIn("COPY assets ./assets", self.dockerfile)

    def test_config_write_endpoint_documented(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("POST /api2/config", readme)
        self.assertIn("config.overrides.json", readme)


class TestControlClientContract(unittest.TestCase):
    """`pctl` must match the unified single-service API."""

    def setUp(self):
        self.pctl = (REPO / "pctl").read_text(encoding="utf-8")

    def test_uses_unified_control_host(self):
        self.assertNotIn("127.0.0.1:8080", self.pctl)
        self.assertNotIn("PIPE_API2_HOST", self.pctl)
        self.assertNotIn("get_api2_host", self.pctl)
        self.assertNotIn("control_api_v2", self.pctl)
        self.assertNotIn("FastAPI", self.pctl)
        self.assertNotIn("ControlHandler", self.pctl)

    def test_uses_supported_config_endpoint(self):
        self.assertIn('post("/api2/config"', self.pctl)
        self.assertNotIn('"/config"', self.pctl.replace('get("/config")', ""))
        self.assertNotIn("config unset", self.pctl)

    def test_does_not_read_control_key_from_pipeline_env(self):
        self.assertNotIn("CONTROL_API_KEY in pipeline.env", self.pctl)
        self.assertIn("/run/secrets/control_api_key", self.pctl)


# Additions appended by the review-fix pass
class TestDeploymentPinnedKeys(unittest.TestCase):
    """Compose-pinned settings must stay a documented, UI-aware set.

    Process env outranks every config layer, so a FIELDS key that compose
    exports into the container can never be changed from the dashboard. The UI
    renders those fields read-only (src/web.rs `pinned_keys`) and the
    deployment pins exactly two: NAS_MEDIA_PREFIX (the bind-mount target) and
    WEBHOOK_PORT (healthcheck + reverse-proxy target).
    """

    def setUp(self):
        self.compose = COMPOSE.read_text(encoding="utf-8")
        self.config = (REPO / "src" / "config.rs").read_text(encoding="utf-8")
        self.web = (REPO / "src" / "web.rs").read_text(encoding="utf-8")

    def _fields_keys(self):
        body = self.config.split("pub const FIELDS", 1)[1].split("\n];", 1)[0]
        return set(re.findall(r'key: "([A-Z0-9_]+)"', body))

    def _compose_env_keys(self):
        """Keys the compose `environment:` block injects, in either YAML form.

        The list form (`- KEY=value`) and the mapping form (`KEY: value`) are
        both valid Compose; recognising only one meant a key silently pinned in
        the other form kept this guard green.
        """
        keys = set()
        in_env = False
        env_indent = -1
        for raw in self.compose.splitlines():
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip())
            if raw.strip().startswith("environment:"):
                env_indent = indent
                in_env = True
                continue
            if not in_env:
                continue
            if indent <= env_indent:  # left the block
                in_env = False
                continue
            stripped = raw.strip()
            # List form, both `- KEY=value` and `- KEY` (value from the caller).
            match = re.match(r"-\s*([A-Z][A-Z0-9_]*)\s*(=|$)", stripped)
            if match:
                keys.add(match.group(1))
                continue
            # Mapping form: KEY: value
            match = re.match(r"([A-Z][A-Z0-9_]*)\s*:", stripped)
            if match:
                keys.add(match.group(1))
        return keys

    def test_compose_env_parser_reads_both_yaml_forms(self):
        # Guard the guard: if this parser only understood one form, the pinned
        # set below could be silently wrong.
        original = self.compose
        try:
            self.compose = (
                "services:\n"
                "  orchestrator:\n"
                "    environment:\n"
                "      TZ: Asia/Jakarta\n"
                "      NAS_MEDIA_PREFIX: /mnt/nas/share/media\n"
                "    volumes:\n"
                "      - /tmp:/tmp\n"
            )
            self.assertEqual(
                self._compose_env_keys(), {"TZ", "NAS_MEDIA_PREFIX"}
            )
            self.compose = (
                "services:\n"
                "  orchestrator:\n"
                "    environment:\n"
                "      - TZ=Asia/Jakarta\n"
                "      - NAS_MEDIA_PREFIX=/mnt/nas/share/media\n"
            )
            self.assertEqual(
                self._compose_env_keys(), {"TZ", "NAS_MEDIA_PREFIX"}
            )
        finally:
            self.compose = original

    def test_pinned_settings_are_exactly_the_documented_pair(self):
        pinned = self._compose_env_keys() & self._fields_keys()
        self.assertEqual(
            pinned,
            {"NAS_MEDIA_PREFIX", "WEBHOOK_PORT"},
            msg="pinning another settings key in compose silently makes it uneditable; "
            "update the pinned set and the UI copy together",
        )

    def test_dashboard_renders_pinned_fields_read_only(self):
        self.assertIn("pub fn env_pinned", self.config)
        self.assertIn("fn pinned_keys", self.web)
        self.assertIn("crate::config::env_pinned", self.web)
        self.assertIn("readonly disabled", self.web)

    def test_healthcheck_follows_the_configured_port(self):
        # A hard-coded port in the healthcheck left the container permanently
        # unhealthy for a deployment that moved WEBHOOK_PORT.
        self.assertNotRegex(self.compose, r"curl[^\n]*127\.0\.0\.1:8085/ready")
        self.assertIn("${WEBHOOK_PORT:-8085}/ready", self.compose)

    def test_ready_counts_resolvable_provider_keys(self):
        api = (REPO / "src" / "api.rs").read_text(encoding="utf-8")
        self.assertIn("llm_keyed", api)
        self.assertRegex(api, r"llm_keyed\s*>\s*0")
        self.assertIn("whisper_keyed", api)
        # A missing bind mount leaves an empty dir behind: is_dir() is not a
        # media check on its own.
        self.assertIn("is_mount_point", api)

    def test_build_context_excludes_operator_secrets(self):
        ignore = (REPO / ".dockerignore").read_text(encoding="utf-8")
        for needle in ("asrsub_providers.json", "secrets/", "*.env"):
            self.assertIn(needle, ignore, msg=f".dockerignore must exclude {needle}")

    def test_override_file_is_written_owner_only(self):
        self.assertIn("mode(0o600)", self.config)
        self.assertIn("set_permissions", self.config)


class TestDocsMatchTheEnvironmentMechanism(unittest.TestCase):
    """The provider-key contract must stay wired, not just documented."""

    def setUp(self):
        self.compose = COMPOSE.read_text(encoding="utf-8")
        self.deploy_md = DEPLOY_MD.read_text(encoding="utf-8") if DEPLOY_MD.exists() else ""
        self.env_example = (REPO / "pipeline.env.example").read_text(encoding="utf-8")

    def test_compose_loads_the_provider_key_environment_file(self):
        # docs/DEPLOY.md promises key_env fallback; without this wiring every
        # keyless provider entry resolves to "" and the pipeline silently
        # performs no ASR or translation at all.
        self.assertIn("env_file:", self.compose)
        self.assertIn("provider_keys.env", self.compose)
        self.assertIn("PROVIDER_KEYS_FILE", self.compose)
        self.assertIn("required: false", self.compose)

    def test_rotation_semantics_are_documented_honestly(self):
        # The process environment is fixed at container start, so no artefact may
        # promise a live reload — including source comments, where the claim
        # survived the first pass.
        sources = [
            self.deploy_md,
            (REPO / "README.md").read_text(encoding="utf-8"),
            (REPO / "src" / "providers.rs").read_text(encoding="utf-8"),
        ]
        for text in sources:
            self.assertNotIn("apply without restart", text)
        self.assertIn("docker compose up -d", self.deploy_md)

    def test_provider_key_verification_prints_names_only(self):
        # `docker compose config` interpolates env_file entries into the rendered
        # environment, so a plain grep in the deploy docs dumps live keys into
        # the operator's terminal.
        self.assertIn("grep -oE '^ *[A-Z_]+_API_KEY:'", self.deploy_md)
        self.assertNotIn("grep -E '^ *[A-Z_]+_API_KEY:'", self.deploy_md)

    def test_docs_state_the_compose_version_the_env_file_form_needs(self):
        # The env_file mapping form with `required:` needs Compose v2.24+.
        self.assertIn("v2.24", self.deploy_md)

    def test_env_only_knobs_are_not_advertised_as_pipeline_env(self):
        # pipeline.env is never exported to the process environment, so
        # env-only knobs there are silently ignored.
        self.assertNotIn("must be set here", self.env_example)
        self.assertIn("CONTAINER environment", self.env_example)


class TestSettingsSchemaMatchesTheLoader(unittest.TestCase):
    """One daemon: the schema, the loader and the docs must describe it alike."""

    def setUp(self):
        self.config_rs = (REPO / "src" / "config.rs").read_text(encoding="utf-8")
        self.env_example = (REPO / "pipeline.env.example").read_text(encoding="utf-8")
        self.deploy_md = DEPLOY_MD.read_text(encoding="utf-8") if DEPLOY_MD.exists() else ""
        fields = re.search(
            r"pub const FIELDS: &\[Field\] = &\[(.*?)\n\];", self.config_rs, re.S
        )
        if fields is None:
            self.fail("FIELDS table not found in src/config.rs")
        self.fields_block = fields.group(1)
        self.keys = re.findall(r'key:\s*"([A-Z0-9_]+)"', self.fields_block)
        env_only = re.search(
            r"pub const ENV_ONLY_KEYS: &\[&str\] = &\[(.*?)\n\];", self.config_rs, re.S
        )
        if env_only is None:
            self.fail("ENV_ONLY_KEYS not found in src/config.rs")
        self.env_only = re.findall(r'"([A-Z0-9_]+)"', env_only.group(1))
        # The merged environment map (`ENV_ALLOWLIST`) and the env-only list both
        # name every settings key, so searching the whole file for a key proves
        # nothing: deleting the loader read stays green as long as the allowlist
        # entry is there. Keep both tables out of the text used below.
        env_allow = re.search(
            r"(?:pub )?const ENV_ALLOWLIST: &\[&str\] = &\[(.*?)\n\];",
            self.config_rs,
            re.S,
        )
        if env_allow is None:
            self.fail("ENV_ALLOWLIST not found in src/config.rs")
        # The file's own unit tests name any number of settings keys, so a
        # deleted loader read would still leave the name behind in `mod tests`.
        # Strip the test module too — the guard is about the *loader*, not about
        # the string appearing somewhere in the file.
        without_tests = re.sub(
            r"\n#\[cfg\(test\)\]\s*\nmod tests \{.*$", "", self.config_rs, flags=re.S
        )
        if without_tests == self.config_rs:
            self.fail("mod tests block not found in src/config.rs")
        self.rest = (
            without_tests.replace(self.fields_block, "")
            .replace(env_allow.group(1), "")
            .replace(env_only.group(1), "")
        )

    def test_finds_the_schema(self):
        # Guards the parsers above: an empty extraction would make the checks
        # below pass vacuously.
        self.assertGreater(len(self.keys), 20)
        self.assertGreater(len(self.env_only), 5)

    def test_every_settings_field_is_read_by_the_loader(self):
        # A FIELDS entry the loader never reads is a knob that does nothing — and
        # the dashboard renders the table, so the UI would offer it. The three
        # tables are excluded (see setUp), and the key must appear in the text
        # between them, i.e. in a loader call or a consumer in this file.
        for key in self.keys:
            self.assertIn(
                f'"{key}"',
                self.rest,
                f"{key} is exposed in FIELDS but never read outside the schema tables",
            )

    def test_env_only_keys_are_documented_in_the_example_env(self):
        # ANILIST_BASE_URL was consumed by jimaku.rs and named in DEPLOY.md but
        # missing here (and from ENV_ONLY_KEYS), so the inventory of env-only
        # knobs was incomplete.
        for key in self.env_only:
            self.assertIn(
                key,
                self.env_example,
                f"{key} is env-only but missing from pipeline.env.example",
            )

    def test_empty_environment_variables_are_documented(self):
        # The rule the code enforces: an empty variable pins nothing and does not
        # shadow a file value. This test only checks that the rule is *documented*
        # where operators will look — the behaviour itself is pinned by the Rust
        # test `empty_env_value_does_not_shadow_the_file_layer`, referenced here
        # so the pair cannot drift apart silently.
        self.assertIn("empty_env_value_does_not_shadow_the_file_layer", self.config_rs)
        for name, text in (
            ("pipeline.env.example", self.env_example),
            ("DEPLOY.md", self.deploy_md),
        ):
            with self.subTest(doc=name):
                self.assertIn("empty", text.lower())
                self.assertIn("pins nothing", text)
                self.assertIn("survives", text)


if __name__ == "__main__":
    unittest.main()
