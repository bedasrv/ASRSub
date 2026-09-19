"""Regression tests for the first simple-deployment safety correction slice."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "asrsub_deploy.py"
TEMPLATE = ROOT / "deploy" / "compose.simple.yaml"
VALID_IMAGE = "ghcr.io/bedasrv/asrsub@sha256:" + "a" * 64


def load_tool():
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


def remote_payload(project: Path) -> dict[str, object]:
    return {
        "expected_hostname": "target-host",
        "project_directory": str(project),
        "compose_file": "compose.yaml",
        "env_file": ".env",
        "service": "orchestrator",
        "project_name": "asrsub",
        "webhook_port": "8085",
        "nas_media_prefix": "/mnt/nas/share/media",
    }


class TestSimpleLayoutSafety(unittest.TestCase):
    def test_candidate_uses_project_relative_data_sources(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for needle in (
            "path: ./secrets/provider_keys.env",
            "source: ./config",
            "target: /home/user/.config/asr-pipeline",
            "source: ./cache",
            "target: /home/user/.cache/asr-pipeline",
            "source: ./state",
            "target: /var/lib/asrsub/state",
            "source: ./secrets",
            "target: /run/secrets",
            "/run/secrets/discord_webhook",
        ):
            self.assertIn(needle, text)
        for legacy_source in (
            "source: /var/lib/asrsub/config",
            "source: /var/lib/asrsub/cache",
            "source: /var/lib/asrsub/runtime-secrets",
        ):
            self.assertNotIn(legacy_source, text)
        self.assertNotIn("control_api_key", text)

    def test_expected_mount_contract_resolves_sources_under_the_project(self):
        remote = load_remote_namespace()
        project = "/opt/mediastack/asrsub"
        expected = set(
            remote["expected_mounts"](
                {
                    "project_directory": project,
                    "nas_media_prefix": "/mnt/nas/share/media",
                }
            )
        )
        self.assertEqual(
            expected,
            {
                (f"{project}/config", "/home/user/.config/asr-pipeline", True),
                (f"{project}/cache", "/home/user/.cache/asr-pipeline", True),
                (f"{project}/state", "/var/lib/asrsub/state", True),
                ("/mnt/nas/share/media", "/mnt/nas/share/media", True),
                ("/mnt/nas/share/media", "/media", False),
                (f"{project}/secrets", "/run/secrets", False),
            },
        )

    def test_project_relative_data_paths_reject_traversal_and_symlink_escape(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            outside = root / "outside"
            project.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            for name in ("config", "cache", "state", "secrets"):
                (project / name).mkdir(mode=0o700)
            payload = {"project_directory": str(project)}
            paths = remote["project_data_paths"](payload)
            self.assertEqual(paths["config"], project / "config")
            self.assertEqual(paths["secrets"], project / "secrets")

            with self.assertRaises(remote["RemoteFailure"]):
                remote["project_relative_path"](project, "../outside")
            with self.assertRaises(remote["RemoteFailure"]):
                remote["project_data_paths"]({"project_directory": str(project / ".." / "project")})

            (project / "config").rmdir()
            (project / "config").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(remote["RemoteFailure"]):
                remote["project_data_paths"](payload)

    def test_inspect_container_reads_repo_digests_from_image_object_by_image_id(self):
        remote = load_remote_namespace()
        image_id = "sha256:" + "b" * 64
        calls = []
        inspected = {
            "{{json .Config.Labels}}": {
                "com.docker.compose.project": "asrsub",
                "com.docker.compose.service": "orchestrator",
            },
            "{{json .Mounts}}": [],
            "{{json .State.Health.Status}}": "healthy",
            "{{json .Config.Image}}": "ghcr.io/bedasrv/asrsub:legacy",
            "{{json .Image}}": image_id,
            "{{json .State.Pid}}": 123,
        }

        def fake_checked(argv, label, **kwargs):
            calls.append((list(argv), label))
            if argv[3:5] == ["image", "inspect"]:
                return json.dumps([VALID_IMAGE])
            return json.dumps(inspected[argv[5]])

        original = remote["checked"]
        remote["checked"] = fake_checked
        try:
            identity = remote["inspect_container"]("container-id")
        finally:
            remote["checked"] = original

        self.assertEqual(identity["repo_digests"], [VALID_IMAGE])
        image_lookup = [argv for argv, _label in calls if argv[3:5] == ["image", "inspect"]]
        self.assertEqual(
            image_lookup,
            [[
                "/usr/bin/docker",
                "--context",
                "default",
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                image_id,
            ]],
        )
        container_inspects = [argv for argv, _label in calls if argv[3] == "inspect"]
        self.assertFalse(any(".RepoDigests" in argument for argv in container_inspects for argument in argv))

    def test_known_home_directory_legacy_mount_layout_is_accepted(self):
        remote = load_remote_namespace()
        identity = {
            "mounts": [
                {"Source": source, "Destination": destination, "RW": rw}
                for source, destination, rw in (
                    ("/home/user/.config/asr-pipeline", "/home/user/.config/asr-pipeline", True),
                    ("/home/user/.cache/asr-pipeline", "/home/user/.cache/asr-pipeline", True),
                    ("/mnt/nas/share/media", "/media", False),
                    ("/mnt/nas/share/media", "/mnt/nas/share/media", True),
                    (
                        "/home/user/.config/asr-pipeline/secrets/control_api_key",
                        "/run/secrets/control_api_key",
                        False,
                    ),
                )
            ]
        }
        self.assertTrue(
            remote["home_legacy_mounts_match"](
                identity,
                {"nas_media_prefix": "/mnt/nas/share/media"},
            )
        )

    def test_home_directory_legacy_mount_layout_rejects_unknown_extra_bind(self):
        remote = load_remote_namespace()
        identity = {
            "mounts": [
                {"Source": source, "Destination": destination, "RW": rw}
                for source, destination, rw in (
                    ("/home/user/.config/asr-pipeline", "/home/user/.config/asr-pipeline", True),
                    ("/home/user/.cache/asr-pipeline", "/home/user/.cache/asr-pipeline", True),
                    ("/mnt/nas/share/media", "/media", False),
                    ("/mnt/nas/share/media", "/mnt/nas/share/media", True),
                    (
                        "/home/user/.config/asr-pipeline/secrets/control_api_key",
                        "/run/secrets/control_api_key",
                        False,
                    ),
                )
            ]
        }
        identity["mounts"].append(
            {
                "Source": "/home/user/.config/asr-pipeline/unknown",
                "Destination": "/opt/unknown",
                "RW": False,
            }
        )
        self.assertFalse(
            remote["home_legacy_mounts_match"](
                identity,
                {"nas_media_prefix": "/mnt/nas/share/media"},
            )
        )

    def test_strict_collect_validates_home_legacy_paths_without_var_lib_requirements(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            for name in ("config", "cache", "state", "secrets"):
                (project / name).mkdir(mode=0o700)
            (project / "compose.yaml").write_text("compose", encoding="utf-8")
            (project / ".env").write_text("WEBHOOK_PORT=8085\n", encoding="utf-8")
            payload = remote_payload(project)
            identity = {
                "labels": {
                    "com.docker.compose.project": "asrsub",
                    "com.docker.compose.service": "orchestrator",
                },
                "mounts": [
                    {"Source": source, "Destination": destination, "RW": rw}
                    for source, destination, rw in (
                        ("/home/user/.config/asr-pipeline", "/home/user/.config/asr-pipeline", True),
                        ("/home/user/.cache/asr-pipeline", "/home/user/.cache/asr-pipeline", True),
                        ("/mnt/nas/share/media", "/media", False),
                        ("/mnt/nas/share/media", "/mnt/nas/share/media", True),
                        (
                            "/home/user/.config/asr-pipeline/secrets/control_api_key",
                            "/run/secrets/control_api_key",
                            False,
                        ),
                    )
                ],
                "health": "healthy",
                "config_image": "ghcr.io/bedasrv/asrsub:legacy",
                "image_id": "image-id",
                "repo_digests": [VALID_IMAGE],
                "pid": 1,
            }
            original = {
                name: remote[name]
                for name in (
                    "metadata",
                    "require_path",
                    "checked",
                    "active_container",
                    "inspect_container",
                    "validate_env_file",
                )
            }
            required_paths = []
            remote["metadata"] = lambda path: {
                "present": True,
                "regular": True,
                "directory": True,
                "symlink": False,
                "mode": 0o700,
                "uid": os.geteuid(),
            }
            def fake_require_path(path, **kwargs):
                required_paths.append(Path(path))
                return {
                    "present": True,
                    "regular": kwargs.get("directory") is False,
                    "directory": kwargs.get("directory") is True,
                    "symlink": False,
                    "mode": 0o600 if kwargs.get("directory") is False else 0o700,
                    "uid": os.geteuid(),
                }

            remote["require_path"] = fake_require_path
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
                with mock.patch.object(remote["socket"], "gethostname", return_value="target-host"), mock.patch.object(remote["socket"], "getfqdn", return_value="target-host"):
                    result = remote["collect"](payload, strict=True)
            finally:
                remote.update(original)

        self.assertEqual(result["checks"]["current_mount_contract"], "legacy-compatible")
        self.assertIn(Path("/home/user/.config/asr-pipeline"), required_paths)
        self.assertIn(Path("/home/user/.cache/asr-pipeline"), required_paths)
        self.assertIn(Path("/home/user/.config/asr-pipeline/secrets"), required_paths)
        self.assertNotIn(Path("/var/lib/asrsub/config"), required_paths)
        self.assertNotIn(Path("/var/lib/asrsub/cache"), required_paths)
        self.assertNotIn(Path("/var/lib/asrsub/state"), required_paths)
        self.assertNotIn(Path("/var/lib/asrsub/runtime-secrets"), required_paths)

    def test_current_known_hardened_legacy_mount_layout_is_accepted(self):
        remote = load_remote_namespace()
        identity = {
            "mounts": [
                {"Source": source, "Destination": destination, "RW": rw}
                for source, destination, rw in (
                    ("/var/lib/asrsub/config", "/home/user/.config/asr-pipeline", True),
                    ("/var/lib/asrsub/cache", "/home/user/.cache/asr-pipeline", True),
                    ("/var/lib/asrsub/state", "/var/lib/asrsub/state", True),
                    ("/mnt/nas/share/media", "/mnt/nas/share/media", True),
                    ("/mnt/nas/share/media", "/media", False),
                    (
                        "/var/lib/asrsub/runtime-secrets/discord_webhook",
                        "/run/secrets/discord_webhook",
                        False,
                    ),
                    (
                        "/home/user/.config/asr-pipeline/secrets/control_api_key",
                        "/run/secrets/control_api_key",
                        False,
                    ),
                    (
                        "/sys/fs/cgroup/system.slice/asrsub-runtime.service/asrsub-children",
                        "/run/asrsub/children-cgroup",
                        True,
                    ),
                    (
                        "/usr/local/libexec/asrsub/asrsub",
                        "/usr/local/bin/asrsub",
                        False,
                    ),
                    (
                        "/var/lib/asrsub/egress-policy/egress-policy.json",
                        "/run/asrsub/egress-policy.json",
                        False,
                    ),
                )
            ]
        }
        self.assertTrue(
            remote["legacy_mounts_match"](
                identity,
                {"nas_media_prefix": "/mnt/nas/share/media"},
            )
        )

    def test_known_legacy_mount_layout_rejects_unknown_extra_bind(self):
        remote = load_remote_namespace()
        identity = {
            "mounts": [
                {"Source": source, "Destination": destination, "RW": rw}
                for source, destination, rw in (
                    ("/var/lib/asrsub/config", "/home/user/.config/asr-pipeline", True),
                    ("/var/lib/asrsub/cache", "/home/user/.cache/asr-pipeline", True),
                    ("/var/lib/asrsub/state", "/var/lib/asrsub/state", True),
                    ("/mnt/nas/share/media", "/mnt/nas/share/media", True),
                    ("/mnt/nas/share/media", "/media", False),
                    (
                        "/home/user/.config/asr-pipeline/secrets/control_api_key",
                        "/run/secrets/control_api_key",
                        False,
                    ),
                )
            ]
        }
        identity["mounts"].append(
            {
                "Source": "/var/lib/asrsub/unknown",
                "Destination": "/opt/unknown",
                "RW": True,
            }
        )
        self.assertFalse(
            remote["legacy_mounts_match"](
                identity,
                {"nas_media_prefix": "/mnt/nas/share/media"},
            )
        )

    def test_unknown_legacy_mount_layout_is_not_accepted(self):
        remote = load_remote_namespace()
        identity = {
            "mounts": [
                {"Source": "/home/user/.config/asr-pipeline", "Destination": "/home/user/.config/asr-pipeline", "RW": True},
                {"Source": "/var/lib/asrsub/cache", "Destination": "/home/user/.cache/asr-pipeline", "RW": True},
                {"Source": "/mnt/nas/share/media", "Destination": "/mnt/nas/share/media", "RW": True},
                {"Source": "/mnt/nas/share/media", "Destination": "/media", "RW": False},
                {"Source": "/var/lib/asrsub/runtime-secrets", "Destination": "/run/secrets", "RW": False},
            ]
        }
        self.assertFalse(remote["legacy_mounts_match"](identity, {"nas_media_prefix": "/mnt/nas/share/media"}))


class TestMediaPrefixSafety(unittest.TestCase):
    def test_local_and_remote_media_prefix_validation_rejects_reserved_overlaps(self):
        deploy = load_tool()
        remote = load_remote_namespace()
        for value in ("/", "relative/media", "/media", "/media/sub", "/run", "/run/secrets", "/home/user/.config", "/home/user/.cache/asr-pipeline"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    deploy.validate_nas_media_prefix(value)
                with self.assertRaises(remote["RemoteFailure"]):
                    remote["validate_nas_media_prefix"](value)
        self.assertEqual(deploy.validate_nas_media_prefix("/mnt/nas/share/media"), "/mnt/nas/share/media")
        self.assertEqual(remote["validate_nas_media_prefix"]("/srv/asrsub/media"), "/srv/asrsub/media")


class TestActiveEnvBoundary(unittest.TestCase):
    def test_non_secret_env_rejects_url_userinfo_and_obvious_inline_secrets(self):
        deploy = load_tool()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            safe = root / "safe.env"
            safe.write_text(
                "METRICS_URL=https://example.invalid/health\n"
                "NAS_MEDIA_PREFIX=/mnt/nas/share/media\n",
                encoding="utf-8",
            )
            deploy.build_candidate_env(
                safe,
                image=VALID_IMAGE,
                webhook_port="8085",
                nas_media_prefix="/mnt/nas/share/media",
            )
            for name, value in (
                ("URL_USERINFO", "https://operator:placeholder@example.invalid/health"),
                ("AUTH_HEADER", "Bearer placeholder-token"),
                ("PEM_VALUE", "-----BEGIN PRIVATE KEY----- placeholder -----END PRIVATE KEY-----"),
                ("QUERY_URL", "https://example.invalid/health?token=placeholder-token"),
            ):
                path = root / f"{name}.env"
                path.write_text(f"{name}={value}\n", encoding="utf-8")
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "secret"):
                    deploy.build_candidate_env(
                        path,
                        image=VALID_IMAGE,
                        webhook_port="8085",
                        nas_media_prefix="/mnt/nas/share/media",
                    )

    def test_public_result_redacts_sensitive_keys_recursively(self):
        deploy = load_tool()
        value = deploy.public_result(
            {
                "safe": "visible",
                "nested": {
                    "password": "placeholder-password",
                    "private-key": "placeholder-private-key",
                    "authorization": "placeholder-authorization",
                    "cookie": "placeholder-cookie",
                    "secret": "placeholder-secret",
                    "token": "placeholder-token",
                    "credential": "placeholder-credential",
                    "webhook": "placeholder-webhook",
                },
                "items": [{"access_token": "placeholder-access-token"}],
            }
        )
        rendered = json.dumps(value)
        self.assertEqual(value["safe"], "visible")
        for sentinel in (
            "placeholder-password",
            "placeholder-private-key",
            "placeholder-authorization",
            "placeholder-cookie",
            "placeholder-secret",
            "placeholder-token",
            "placeholder-credential",
            "placeholder-webhook",
            "placeholder-access-token",
        ):
            self.assertNotIn(sentinel, rendered)

    def test_strict_collect_validates_the_active_env_before_success(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            for name in ("config", "cache", "state", "secrets"):
                (project / name).mkdir(mode=0o700)
            (project / "compose.yaml").write_text("compose", encoding="utf-8")
            (project / ".env").write_text("WEBHOOK_PORT=8085\n", encoding="utf-8")
            payload = remote_payload(project)
            original = {name: remote[name] for name in ("require_path", "metadata", "checked", "active_container", "inspect_container", "validate_env_file")}
            called = mock.Mock()
            required_paths = []
            remote["validate_env_file"] = called
            def fake_metadata(path):
                is_project = Path(path) == project
                return {"present": True, "regular": not is_project, "directory": is_project, "symlink": False, "mode": 0o700 if is_project else 0o600, "uid": os.geteuid()}
            remote["metadata"] = fake_metadata
            def fake_require_path(path, **kwargs):
                required_paths.append(Path(path))
                return {"present": True, "regular": kwargs.get("directory") is False, "directory": kwargs.get("directory") is True, "symlink": False, "mode": 0o700, "uid": os.geteuid()}

            remote["require_path"] = fake_require_path
            remote["checked"] = lambda argv, label, **kwargs: (
                'LISTEN 0 4096 0.0.0.0:8085 0.0.0.0:* users:(("asrsub",pid=1,fd=1))\n'
                if argv[0] == "ss"
                else "/mnt/nas/share nfs4 nas.example:/exports/media\n"
                if argv[0] == "findmnt"
                else "ok\n"
            )
            remote["active_container"] = lambda value: ("container-id", {})
            remote["inspect_container"] = lambda value: {
                "labels": {"com.docker.compose.project": "asrsub", "com.docker.compose.service": "orchestrator"},
                "mounts": [
                    {"Source": source, "Destination": destination, "RW": rw}
                    for source, destination, rw in remote["expected_mounts"](payload)
                ],
                "health": "healthy",
                "config_image": VALID_IMAGE,
                "image_id": "image-id",
                "repo_digests": [VALID_IMAGE],
                "pid": 1,
            }
            try:
                with mock.patch.object(remote["socket"], "gethostname", return_value="target-host"), mock.patch.object(remote["socket"], "getfqdn", return_value="target-host"):
                    remote["collect"](payload, strict=True)
            finally:
                remote.update(original)
            called.assert_called_once_with(project / ".env")
            self.assertIn(project / "state", required_paths)


class TestTemplateAndHostnameBoundaries(unittest.TestCase):
    def test_payload_rejects_an_adversarial_compose_source_before_encoding(self):
        deploy = load_tool()
        config = deploy.DeploymentConfig(target="target.example", expected_hostname="target-host")
        with tempfile.TemporaryDirectory() as directory:
            evil = Path(directory) / "evil.yaml"
            evil.write_text(
                "services:\n  privileged:\n    image: attacker\n    privileged: true\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "tracked"):
                deploy._payload_for_deploy(config, VALID_IMAGE, evil, None)

    def test_wrong_hostname_rejects_before_lock_creation_and_leaves_project_unchanged(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            before = sorted(project.iterdir())
            payload = remote_payload(project)
            original_hostname = remote["socket"].gethostname
            original_fqdn = remote["socket"].getfqdn
            remote["socket"].gethostname = lambda: "actual-host"
            remote["socket"].getfqdn = lambda: "actual-host.example"
            try:
                with self.assertRaises(remote["RemoteFailure"]):
                    remote["deploy"](payload)
            finally:
                remote["socket"].gethostname = original_hostname
                remote["socket"].getfqdn = original_fqdn
            self.assertEqual(sorted(project.iterdir()), before)
            self.assertFalse((project / ".asrsub-deploy.lock").exists())

    def test_status_preflight_and_rollback_check_hostname_before_any_project_operation(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            payload = remote_payload(project)
            original_hostname = remote["socket"].gethostname
            original_fqdn = remote["socket"].getfqdn
            remote["socket"].gethostname = lambda: "actual-host"
            remote["socket"].getfqdn = lambda: "actual-host.example"
            try:
                for strict in (False, True):
                    with self.subTest(operation="preflight" if strict else "status"), self.assertRaises(remote["RemoteFailure"]):
                        remote["collect"](payload, strict=strict)
                with self.assertRaises(remote["RemoteFailure"]):
                    remote["rollback"](payload)
            finally:
                remote["socket"].gethostname = original_hostname
                remote["socket"].getfqdn = original_fqdn
            self.assertEqual(list(project.iterdir()), [])


class TestManagedProjectRole(unittest.TestCase):
    def test_mutation_lock_rejects_foreign_group_writable_symlink_and_wrong_type_projects(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = remote_payload(root / "project")

            project = root / "project"
            project.mkdir(mode=0o700)
            project.chmod(0o770)
            with self.assertRaises(remote["RemoteFailure"]):
                with remote["mutation_lock"](payload, timeout=0.1):
                    pass
            project.chmod(0o700)

            original_metadata = remote["metadata"]
            def foreign_owner(path):
                info = original_metadata(path)
                if Path(path) == project:
                    info["uid"] = os.geteuid() + 1
                return info
            remote["metadata"] = foreign_owner
            try:
                with self.assertRaises(remote["RemoteFailure"]):
                    with remote["mutation_lock"](payload, timeout=0.1):
                        pass
            finally:
                remote["metadata"] = original_metadata

            project.rename(root / "real-project")
            link = root / "project"
            link.symlink_to(root / "real-project", target_is_directory=True)
            with self.assertRaises(remote["RemoteFailure"]):
                with remote["mutation_lock"]({**payload, "project_directory": str(link)}, timeout=0.1):
                    pass
            link.unlink()
            (root / "project").write_text("not a directory", encoding="utf-8")
            with self.assertRaises(remote["RemoteFailure"]):
                with remote["mutation_lock"](payload, timeout=0.1):
                    pass


class TestDockerHealthGate(unittest.TestCase):
    def test_docker_health_poll_accepts_starting_then_healthy(self):
        remote = load_remote_namespace()
        payload = {"project_directory": "/opt/mediastack/asrsub", "service": "orchestrator"}
        original_active = remote["active_container"]
        original_inspect = remote["inspect_container"]
        original_sleep = remote["time"].sleep
        statuses = iter(("starting", "healthy"))
        remote["active_container"] = lambda value: ("container-id", {})
        remote["inspect_container"] = lambda value: {"health": next(statuses)}
        remote["time"].sleep = lambda value: None
        try:
            identity = remote["wait_docker_health"](payload, deadline=remote["time"].monotonic() + 5)
        finally:
            remote["active_container"] = original_active
            remote["inspect_container"] = original_inspect
            remote["time"].sleep = original_sleep
        self.assertEqual(identity["health"], "healthy")

    def test_docker_health_poll_fails_on_unhealthy_and_times_out_when_stuck_starting(self):
        remote = load_remote_namespace()
        payload = {"project_directory": "/opt/mediastack/asrsub", "service": "orchestrator"}
        original_active = remote["active_container"]
        original_inspect = remote["inspect_container"]
        original_sleep = remote["time"].sleep
        original_monotonic = remote["time"].monotonic
        remote["active_container"] = lambda value: ("container-id", {})
        remote["inspect_container"] = lambda value: {"health": "unhealthy"}
        try:
            with self.assertRaises(remote["RemoteFailure"]):
                remote["wait_docker_health"](payload, deadline=remote["time"].monotonic() + 5)
            clock = iter((0.0, 0.0, 2.0))
            remote["time"].monotonic = lambda: next(clock)
            remote["inspect_container"] = lambda value: {"health": "starting"}
            remote["time"].sleep = lambda value: None
            with self.assertRaises(remote["RemoteFailure"]):
                remote["wait_docker_health"](payload, timeout=1)
        finally:
            remote["active_container"] = original_active
            remote["inspect_container"] = original_inspect
            remote["time"].sleep = original_sleep
            remote["time"].monotonic = original_monotonic


class TestRollbackHashNormalization(unittest.TestCase):
    @staticmethod
    def _write_backup(root: Path, name: str, **overrides) -> Path:
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
            "previous_mount_contract": [{"source": "/var/lib/asrsub/config", "destination": "/home/user/.config/asr-pipeline", "rw": True}],
            "compose_file": "compose.yaml",
            "env_file": ".env",
            "compose_sha256": hashlib.sha256(compose).hexdigest(),
            "env_sha256": hashlib.sha256(env).hexdigest(),
        }
        record.update(overrides)
        (backup / "metadata.json").write_text(json.dumps(record), encoding="utf-8")
        for filename in ("compose.yaml", ".env", "metadata.json"):
            (backup / filename).chmod(0o600)
        return backup

    def test_local_backup_selection_skips_null_numeric_missing_and_malformed_hash_fields(self):
        deploy = load_tool()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "20260101T000000Z"
            self._write_backup(root, valid.name)
            candidates = [valid]
            for index, value in enumerate((None, 123, "bad", "f" * 63, "g" * 64), 2):
                path = self._write_backup(root, f"2026010{index}T000000Z", compose_sha256=value)
                candidates.append(path)
            missing = self._write_backup(root, "20260108T000000Z")
            missing_record = json.loads((missing / "metadata.json").read_text(encoding="utf-8"))
            del missing_record["compose_sha256"]
            (missing / "metadata.json").write_text(json.dumps(missing_record), encoding="utf-8")
            candidates.append(missing)
            self.assertEqual(deploy.select_latest_verified_backup(candidates), valid)

    def test_remote_rollback_selection_normalizes_malformed_hash_fields_to_remote_failure(self):
        remote = load_remote_namespace()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir(mode=0o700)
            root = project / ".asrsub-rollback"
            root.mkdir(mode=0o700)
            payload = remote_payload(project)
            for index, value in enumerate((None, 123, "bad", "f" * 63, "g" * 64), 1):
                backup = self._write_backup(root, f"2026010{index}T000000Z", compose_sha256=value)
                with self.subTest(value=value):
                    with self.assertRaises(remote["RemoteFailure"]):
                        remote["read_backup_record"](payload, backup)
            missing = self._write_backup(root, "20260108T000000Z")
            missing_record = json.loads((missing / "metadata.json").read_text(encoding="utf-8"))
            del missing_record["env_sha256"]
            (missing / "metadata.json").write_text(json.dumps(missing_record), encoding="utf-8")
            with self.assertRaises(remote["RemoteFailure"]):
                remote["read_backup_record"](payload, missing)
            with self.assertRaises(remote["RemoteFailure"]):
                remote["latest_backup"](payload)


if __name__ == "__main__":
    unittest.main()
