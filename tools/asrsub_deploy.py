#!/usr/bin/env python3
"""Simple immutable Compose deployment for ASRSub.

The local process validates inputs and streams one standard-library Python
program to the target over ``ssh target python3 -``.  The remote program owns
all target-side checks and mutations so no nested shell command is assembled.
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as _datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


IMAGE_RE = re.compile(r"^ghcr\.io/bedasrv/asrsub@sha256:[0-9a-f]{64}$")
PORT_RE = re.compile(r"^[1-9][0-9]{0,4}$")
DEFAULT_PROJECT_DIRECTORY = "/opt/mediastack/asrsub"
DEFAULT_COMPOSE_FILE = "compose.yaml"
DEFAULT_ENV_FILE = ".env"
DEFAULT_SERVICE = "orchestrator"
DEFAULT_PROJECT_NAME = "asrsub"
DEFAULT_WEBHOOK_PORT = "8085"
DEFAULT_NAS_MEDIA_PREFIX = "/mnt/nas/share/media"
DEFAULT_PROVIDER_KEYS_FILE = "/home/user/.config/asr-pipeline/secrets/provider_keys.env"
DEFAULT_LISTENER_PROCESS = "asrsub"
DEFAULT_TEMPLATE = Path(__file__).resolve().parents[1] / "deploy" / "compose.simple.yaml"
MANAGED_ENV_KEYS = frozenset(
    {"ASRSUB_IMAGE", "WEBHOOK_PORT", "NAS_MEDIA_PREFIX", "PROVIDER_KEYS_FILE"}
)
SENSITIVE_ENV_KEY = re.compile(
    r"(?:^|_)(?:API_KEY|PASSWORD|TOKEN|SECRET|PRIVATE_KEY|WEBHOOK|CREDENTIALS?)(?:_|$)"
)
FORBIDDEN_COMMAND_TOKENS = frozenset(
    {"down", "prune", "rm", "restart", "systemctl", "daemon-reload"}
)
DROPPED_RESULT_KEYS = frozenset(
    {"stdout", "stderr", "secret", "secrets", "environment", "env", "raw", "raw_output"}
)


class DeployError(Exception):
    """A local validation, transport, or structured remote-operation error."""


@dataclasses.dataclass(frozen=True)
class DeploymentConfig:
    target: str
    expected_hostname: str
    project_directory: str = DEFAULT_PROJECT_DIRECTORY
    compose_file: str = DEFAULT_COMPOSE_FILE
    env_file: str = DEFAULT_ENV_FILE
    service: str = DEFAULT_SERVICE
    project_name: str = DEFAULT_PROJECT_NAME
    webhook_port: str = DEFAULT_WEBHOOK_PORT
    nas_media_prefix: str = DEFAULT_NAS_MEDIA_PREFIX
    timeout: float = 120.0


def validate_image_reference(value: str) -> str:
    """Accept only the one repository's lowercase, 64-hex immutable digest."""
    if not isinstance(value, str) or IMAGE_RE.fullmatch(value) is None:
        raise ValueError(
            "image must exactly match ghcr.io/bedasrv/asrsub@sha256:<64 lowercase hex>"
        )
    return value


def _validate_port(value: str) -> str:
    if not isinstance(value, str) or PORT_RE.fullmatch(value) is None:
        raise ValueError("WEBHOOK_PORT must be a decimal TCP port")
    number = int(value)
    if number > 65535:
        raise ValueError("WEBHOOK_PORT is outside the TCP port range")
    return value


def parse_port_ownership(
    socket_listing: str,
    port: str,
    *,
    expected_process: str = DEFAULT_LISTENER_PROCESS,
) -> dict[str, Any]:
    """Return bounded ownership metadata for one LISTEN port, never raw ss output."""
    port = _validate_port(port)
    names: set[str] = set()
    listening = False
    for line in socket_listing.splitlines():
        fields = line.split()
        if not fields or fields[0] != "LISTEN" or len(fields) < 4:
            continue
        local_endpoint = fields[3]
        if local_endpoint.rsplit(":", 1)[-1] != port:
            continue
        listening = True
        names.update(re.findall(r'\(\(\"([^\"]+)\"', line))
    ordered_names = sorted(names)
    owned = expected_process in names
    return {
        "port": port,
        "status": "owned" if owned else "unrelated" if listening else "not_listening",
        "process_name": expected_process if owned else (ordered_names[0] if ordered_names else None),
        "process_names": ordered_names,
    }


def _safe_text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{name} must be a non-empty single-line value")
    return value


def _remote_path(project_directory: str, value: str) -> str:
    project = _safe_text(project_directory, name="project directory")
    if not project.startswith("/"):
        raise ValueError("project directory must be absolute")
    child = _safe_text(value, name="path")
    if os.path.isabs(child):
        return child
    if child in {".", ".."} or "/" in child or "\\" in child:
        raise ValueError("compose and env file names must be single path components")
    return os.path.join(project, child)


def compose_command(
    project_directory: str,
    compose_file: str,
    env_file: str,
    *operation: str,
) -> list[str]:
    """Build an allowlisted Compose argv; never invoke a shell or destructive verb."""
    if not operation:
        raise ValueError("Compose operation is required")
    if any(token in FORBIDDEN_COMMAND_TOKENS for token in operation):
        raise ValueError("destructive Compose/system operation is not allowed")
    if operation[0] not in {"config", "pull", "up", "ps"}:
        raise ValueError("Compose operation is not allowlisted")
    project = _safe_text(project_directory, name="project directory")
    if not project.startswith("/"):
        raise ValueError("project directory must be absolute")
    return [
        "docker",
        "compose",
        "--project-directory",
        project,
        "--env-file",
        _remote_path(project, env_file),
        "-f",
        _remote_path(project, compose_file),
        *operation,
    ]


def ssh_command(target: str) -> list[str]:
    """Return the fixed SSH argv used to stream the remote Python program."""
    target = _safe_text(target, name="target")
    if target.startswith("-"):
        raise ValueError("target must not be an SSH option")
    return ["ssh", "-T", "-o", "BatchMode=yes", target, "python3", "-"]


def _result_value(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in DROPPED_RESULT_KEYS or "api_key" in lowered or lowered.endswith("_token"):
                continue
            result[str(key)] = _result_value(child)
        return result
    if isinstance(value, list):
        return [_result_value(item) for item in value]
    if isinstance(value, tuple):
        return [_result_value(item) for item in value]
    return value


def public_result(value: dict[str, Any]) -> dict[str, Any]:
    """Keep structured status while dropping raw streams and sensitive fields."""
    sanitized = _result_value(value)
    return sanitized if isinstance(sanitized, dict) else {"ok": False, "error": "invalid result"}


def _looks_sensitive_path(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    forbidden_fragments = ("secret", "credential", "password", "private_key", "provider_key")
    return any(fragment in part for part in parts for fragment in forbidden_fragments) or path.name.lower() in {
        "control_api_key",
        "discord_webhook",
    }


def _validate_non_secret_env(text: str) -> None:
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"non-secret env line {line_number} is not KEY=value")
        key, _value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"non-secret env line {line_number} has an invalid key")
        if SENSITIVE_ENV_KEY.search(key) and key not in {"WEBHOOK_PORT"}:
            raise ValueError(f"secret-bearing env key is not accepted: {key}")


def build_candidate_env(
    source: Path | None,
    *,
    image: str,
    webhook_port: str,
    nas_media_prefix: str,
    provider_keys_file: str = DEFAULT_PROVIDER_KEYS_FILE,
) -> str:
    """Build only non-secret interpolation values; secret files are never read."""
    validate_image_reference(image)
    webhook_port = _validate_port(webhook_port)
    nas_media_prefix = _safe_text(nas_media_prefix, name="NAS_MEDIA_PREFIX")
    provider_keys_file = _safe_text(provider_keys_file, name="PROVIDER_KEYS_FILE")
    lines: list[str] = []
    if source is not None:
        source = Path(source)
        if _looks_sensitive_path(source):
            raise ValueError("--env-source must name a non-secret env file")
        text = source.read_text(encoding="utf-8")
        _validate_non_secret_env(text)
        for line in text.splitlines():
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key not in MANAGED_ENV_KEYS:
                lines.append(line)
    lines.extend(
        [
            f"ASRSUB_IMAGE={image}",
            f"WEBHOOK_PORT={webhook_port}",
            f"NAS_MEDIA_PREFIX={nas_media_prefix}",
            f"PROVIDER_KEYS_FILE={provider_keys_file}",
        ]
    )
    return "\n".join(lines) + "\n"


def validate_template_text(text: str) -> None:
    required = (
        "${ASRSUB_IMAGE:?",
        "network_mode: host",
        "restart: unless-stopped",
        "WEBHOOK_PORT",
        "NAS_MEDIA_PREFIX",
        "required: false",
        "/run/secrets",
    )
    if any(value not in text for value in required):
        raise ValueError("simple Compose template is missing an approved contract")
    if re.search(r"(?m)^secrets:\s*$", text):
        raise ValueError("simple Compose template must not declare top-level Compose secrets")
    for forbidden in ("cgroup", "egress-policy", "/usr/local/libexec", "systemd"):
        if forbidden in text:
            raise ValueError("simple Compose template contains retired hardened runtime wiring")


def remote_program(operation: str, payload: dict[str, Any]) -> str:
    """Embed a JSON payload in one streamed Python source file, not shell syntax."""
    encoded = base64.b64encode(
        json.dumps({"operation": operation, **payload}, sort_keys=True, separators=(",", ":")).encode()
    ).decode("ascii")
    return REMOTE_SCRIPT + "\nPAYLOAD_B64 = " + repr(encoded) + "\nmain(PAYLOAD_B64)\n"


@dataclasses.dataclass(frozen=True)
class SSHResult:
    returncode: int
    stdout: str
    stderr: str


def stream_ssh(command: Sequence[str], script: str, timeout: float) -> SSHResult:
    """Stream one script to SSH and retain output only for structured parsing."""
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise DeployError("could not start SSH") from exc
    try:
        stdout, stderr = process.communicate(script, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise DeployError("SSH operation exceeded its bounded timeout") from exc
    return SSHResult(process.returncode, stdout, stderr)


def _parse_structured_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "ok" in value:
            return value
    raise DeployError("remote operation returned no structured result")


def run_remote(
    config: DeploymentConfig,
    operation: str,
    payload: dict[str, Any] | None = None,
    *,
    runner: Callable[[Sequence[str], str, float], SSHResult] = stream_ssh,
) -> dict[str, Any]:
    if operation not in {"status", "preflight", "deploy", "rollback"}:
        raise ValueError("operation is not allowlisted")
    merged = {
        "expected_hostname": config.expected_hostname,
        "project_directory": config.project_directory,
        "compose_file": config.compose_file,
        "env_file": config.env_file,
        "service": config.service,
        "project_name": config.project_name,
        "webhook_port": config.webhook_port,
        "nas_media_prefix": config.nas_media_prefix,
        **(payload or {}),
    }
    result = runner(ssh_command(config.target), remote_program(operation, merged), config.timeout)
    try:
        structured = _parse_structured_result(result.stdout)
    except DeployError:
        if result.returncode:
            raise DeployError("remote operation failed before returning structured status")
        raise
    structured = public_result(structured)
    if result.returncode and structured.get("ok") is True:
        structured["ok"] = False
        structured["error"] = "remote transport returned a failure status"
    return structured


def select_latest_verified_backup(backups: Iterable[Path]) -> Path:
    """Select the newest verified backup whose recorded image is an exact digest."""
    candidates: list[tuple[str, str, Path]] = []
    for raw_path in backups:
        path = Path(raw_path)
        if not path.is_dir() or path.is_symlink():
            continue
        try:
            metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict) or metadata.get("verified") is not True:
            continue
        image = metadata.get("previous_image")
        if not isinstance(image, str) or IMAGE_RE.fullmatch(image) is None:
            continue
        created = metadata.get("created_at")
        if not isinstance(created, str):
            continue
        candidates.append((created, path.name, path))
    if not candidates:
        raise DeployError("no verified immutable rollback backup is available")
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _payload_for_deploy(config: DeploymentConfig, image: str, template: Path, env_source: Path | None) -> dict[str, Any]:
    validate_image_reference(image)
    compose_text = template.read_text(encoding="utf-8")
    validate_template_text(compose_text)
    env_text = build_candidate_env(
        env_source,
        image=image,
        webhook_port=config.webhook_port,
        nas_media_prefix=config.nas_media_prefix,
    )
    return {
        "image": image,
        "compose_text": base64.b64encode(compose_text.encode("utf-8")).decode("ascii"),
        "env_text": base64.b64encode(env_text.encode("utf-8")).decode("ascii"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("status", "preflight", "deploy", "rollback"))
    parser.add_argument("--target", required=True, help="SSH target, for example user@host")
    parser.add_argument("--expected-hostname", required=True)
    parser.add_argument("--image", help="exact ghcr.io/bedasrv/asrsub@sha256 digest for deploy")
    parser.add_argument("--project-directory", default=DEFAULT_PROJECT_DIRECTORY)
    parser.add_argument("--compose-file", default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--webhook-port", default=DEFAULT_WEBHOOK_PORT)
    parser.add_argument("--nas-media-prefix", default=DEFAULT_NAS_MEDIA_PREFIX)
    parser.add_argument("--compose-source", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--env-source", type=Path)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = DeploymentConfig(
            target=args.target,
            expected_hostname=args.expected_hostname,
            project_directory=args.project_directory,
            compose_file=args.compose_file,
            env_file=args.env_file,
            service=args.service,
            project_name=args.project_name,
            webhook_port=_validate_port(args.webhook_port),
            nas_media_prefix=args.nas_media_prefix,
            timeout=args.timeout,
        )
        payload: dict[str, Any] = {}
        if args.image is not None:
            validate_image_reference(args.image)
        if args.operation == "deploy":
            if args.image is None:
                raise ValueError("deploy requires --image with an exact immutable digest")
            payload = _payload_for_deploy(config, args.image, args.compose_source, args.env_source)
        result = run_remote(config, args.operation, payload)
        print(json.dumps(public_result(result), sort_keys=True, separators=(",", ":")))
        return 0 if result.get("ok") is True else 2
    except (DeployError, OSError, UnicodeError, ValueError) as exc:
        # Never echo remote stderr, env files, or exception payloads.  A caller
        # can use the bounded operation status and target-side logs separately.
        _ = exc
        print("asrsub-deploy: operation failed; no secret values were printed", file=sys.stderr)
        return 2


REMOTE_SCRIPT = r'''#!/usr/bin/env python3
from __future__ import annotations

import base64
import datetime as datetime_module
import json
import os
import re
import socket
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


IMAGE_RE = re.compile(r"^ghcr\.io/bedasrv/asrsub@sha256:[0-9a-f]{64}$")
FORBIDDEN = frozenset({"down", "prune", "rm", "restart", "systemctl", "daemon-reload"})
EXPECTED_LISTENER_PROCESS = "asrsub"


class RemoteFailure(Exception):
    pass


def immutable(value):
    return isinstance(value, str) and IMAGE_RE.fullmatch(value) is not None


def checked(argv, label, *, cwd=None, timeout=60):
    argv = [str(item) for item in argv]
    if any(item in FORBIDDEN for item in argv):
        raise RemoteFailure("forbidden operation")
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RemoteFailure(label) from exc
    if result.returncode != 0:
        raise RemoteFailure(label)
    return result.stdout


def project_paths(payload):
    project = Path(payload["project_directory"])
    if not project.is_absolute() or project.is_symlink() or not project.is_dir():
        raise RemoteFailure("project directory is not a real directory")
    compose_name = payload["compose_file"]
    env_name = payload["env_file"]
    if any(not isinstance(name, str) or not name or os.path.isabs(name) or "/" in name or "\\" in name or name in {".", ".."} for name in (compose_name, env_name)):
        raise RemoteFailure("compose and env paths must be single file names")
    compose = project / compose_name
    env = project / env_name
    return project, compose, env


def metadata(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"present": False}
    except OSError:
        return {"present": False, "unreadable": True}
    return {
        "present": True,
        "symlink": stat.S_ISLNK(info.st_mode),
        "regular": stat.S_ISREG(info.st_mode),
        "directory": stat.S_ISDIR(info.st_mode),
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "size": info.st_size,
    }


def require_path(path, *, directory=None):
    info = metadata(path)
    if not info.get("present") or info.get("symlink"):
        raise RemoteFailure("required path is absent or unsafe")
    if directory is True and not info.get("directory"):
        raise RemoteFailure("required directory is not a directory")
    if directory is False and not info.get("regular"):
        raise RemoteFailure("required file is not regular")
    return info


def env_values(path):
    values = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key in {"WEBHOOK_PORT", "NAS_MEDIA_PREFIX"}:
                    values[key] = value
    except (OSError, UnicodeError) as exc:
        raise RemoteFailure("non-secret env file cannot be read") from exc
    return values


def compose_argv(payload, *operation):
    project, compose, env = project_paths(payload)
    allowed = {"config", "pull", "up", "ps"}
    if not operation or operation[0] not in allowed or any(item in FORBIDDEN for item in operation):
        raise RemoteFailure("Compose operation is not allowlisted")
    return [
        "docker", "compose",
        "--project-directory", str(project),
        "--env-file", str(env),
        "-f", str(compose),
        "-p", payload["project_name"],
        *operation,
    ]


def json_records(text):
    text = text.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
        return value if isinstance(value, list) else [value]
    except json.JSONDecodeError:
        records = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records


def active_container(payload):
    rows = json_records(checked(compose_argv(payload, "ps", "--format", "json"), "Compose status", cwd=Path(payload["project_directory"])))
    service = payload["service"]
    selected = next((row for row in rows if row.get("Service", row.get("service")) == service), None)
    if not isinstance(selected, dict):
        raise RemoteFailure("active Compose service is not owned by this project")
    container = selected.get("ID", selected.get("Id", selected.get("id", selected.get("ContainerID"))))
    if not isinstance(container, str) or not container:
        raise RemoteFailure("active Compose service has no container identity")
    return container, selected


def inspect_container(container):
    def inspect(expression, label):
        output = checked(["docker", "inspect", "--format", expression, container], label)
        try:
            return json.loads(output.strip())
        except json.JSONDecodeError as exc:
            raise RemoteFailure(label) from exc
    labels = inspect("{{json .Config.Labels}}", "container labels") or {}
    mounts = inspect("{{json .Mounts}}", "container mounts") or []
    health = inspect("{{json .State.Health.Status}}", "container health")
    config_image = inspect("{{json .Config.Image}}", "container image reference")
    image_id = inspect("{{json .Image}}", "container image id")
    repo_digests = inspect("{{json .RepoDigests}}", "container image digests") or []
    return {
        "labels": labels if isinstance(labels, dict) else {},
        "mounts": mounts if isinstance(mounts, list) else [],
        "health": health,
        "config_image": config_image,
        "image_id": image_id,
        "repo_digests": repo_digests if isinstance(repo_digests, list) else [],
    }


def immutable_repo_digest(identity):
    for value in identity.get("repo_digests", []):
        if immutable(value):
            return value
    return None


def immutable_active_image(identity):
    digest = immutable_repo_digest(identity)
    if digest is None:
        raise RemoteFailure("active image has no recoverable immutable RepoDigest")
    return digest


def parse_port_ownership(socket_listing, port, expected_process=EXPECTED_LISTENER_PROCESS):
    names = set()
    listening = False
    for line in socket_listing.splitlines():
        fields = line.split()
        if not fields or fields[0] != "LISTEN" or len(fields) < 4:
            continue
        if fields[3].rsplit(":", 1)[-1] != port:
            continue
        listening = True
        names.update(re.findall(r'\(\("([^"]+)"', line))
    ordered_names = sorted(names)
    owned = expected_process in names
    return {
        "port": port,
        "status": "owned" if owned else "unrelated" if listening else "not_listening",
        "process_name": expected_process if owned else (ordered_names[0] if ordered_names else None),
        "process_names": ordered_names,
    }


def expected_mounts(payload):
    prefix = payload["nas_media_prefix"]
    media = "/mnt/nas/share/media"
    return [
        ("/home/user/.config/asr-pipeline", "/home/user/.config/asr-pipeline", True),
        ("/home/user/.cache/asr-pipeline", "/home/user/.cache/asr-pipeline", True),
        (media, prefix, True),
        (media, "/media", False),
        ("/home/user/.config/asr-pipeline/secrets", "/run/secrets", False),
    ]


def mount_contract(identity):
    contract = []
    for mount in identity.get("mounts", []):
        if not isinstance(mount, dict):
            raise RemoteFailure("container mount metadata is invalid")
        source = mount.get("Source")
        destination = mount.get("Destination")
        if not isinstance(source, str) or not isinstance(destination, str):
            raise RemoteFailure("container mount metadata is invalid")
        contract.append({"source": source, "destination": destination, "rw": bool(mount.get("RW"))})
    return contract


def contract_tuples(contract):
    if not isinstance(contract, list):
        return []
    values = []
    for mount in contract:
        if not isinstance(mount, dict):
            return []
        source = mount.get("source")
        destination = mount.get("destination")
        if not isinstance(source, str) or not isinstance(destination, str):
            return []
        values.append((source, destination, bool(mount.get("rw"))))
    return values


def mount_contract_matches(identity, saved_contract):
    actual = mount_contract(identity)
    expected = contract_tuples(saved_contract)
    return len(actual) == len(expected) and set(contract_tuples(actual)) == set(expected)


def mounts_match(identity, payload):
    actual = contract_tuples(mount_contract(identity))
    expected = expected_mounts(payload)
    return len(actual) == len(expected) and set(actual) == set(expected)


def collect(payload, *, strict):
    project, compose, env = project_paths(payload)
    paths = {
        "project": require_path(project, directory=True),
        "compose": require_path(compose, directory=False),
        "env": require_path(env, directory=False),
    }
    for name, path in (
        ("config", Path("/home/user/.config/asr-pipeline")),
        ("cache", Path("/home/user/.cache/asr-pipeline")),
        ("media", Path("/mnt/nas/share/media")),
        ("secrets", Path("/home/user/.config/asr-pipeline/secrets")),
    ):
        paths[name] = require_path(path, directory=True)
    secret_metadata = {
        name: metadata(Path("/home/user/.config/asr-pipeline/secrets") / name)
        for name in ("control_api_key", "discord_webhook", "provider_keys.env")
    }
    if strict:
        if paths["secrets"]["mode"] & 0o077:
            raise RemoteFailure("secret directory permissions are too broad")
        control = secret_metadata["control_api_key"]
        if not control.get("present") or not control.get("regular") or control.get("symlink") or control.get("mode", 0) & 0o077:
            raise RemoteFailure("control secret metadata is not safe")
        for name in ("discord_webhook", "provider_keys.env"):
            optional = secret_metadata[name]
            if optional.get("present") and (not optional.get("regular") or optional.get("symlink") or optional.get("mode", 0) & 0o077):
                raise RemoteFailure("optional secret metadata is not safe")
    checked(["docker", "version", "--format", "{{.Server.Version}}"], "Docker version")
    checked(["docker", "compose", "version", "--short"], "Compose version")
    checked(["findmnt", "-T", "/mnt/nas/share/media", "-n", "-o", "TARGET"], "media mount")
    port = env_values(env).get("WEBHOOK_PORT", payload["webhook_port"])
    if not re.fullmatch(r"[1-9][0-9]{0,4}", port or "") or int(port) > 65535:
        raise RemoteFailure("WEBHOOK_PORT is invalid")
    socket_listing = checked(["ss", "-ltnp"], "port ownership")
    port_ownership = parse_port_ownership(socket_listing, port)
    port_owned = port_ownership["status"] == "owned"
    container, row = active_container(payload)
    identity = inspect_container(container)
    owned = (
        identity["labels"].get("com.docker.compose.project") == payload["project_name"]
        and identity["labels"].get("com.docker.compose.service") == payload["service"]
    )
    current_mounts = mounts_match(identity, payload)
    previous_repo_digest = immutable_repo_digest(identity)
    if strict and not owned:
        raise RemoteFailure("active container project/service ownership does not match")
    if strict and not port_owned:
        raise RemoteFailure("configured port is not owned by the intended ASRSub listener")
    if strict and identity.get("health") != "healthy":
        raise RemoteFailure("active container is not healthy")
    if strict and previous_repo_digest is None:
        raise RemoteFailure("active service has no recoverable immutable RepoDigest")
    current_mount_contract = "simple" if current_mounts else "legacy-compatible"
    return {
        "ok": True,
        "checks": {
            "docker_compose": True,
            "project_service_owned": owned,
            "required_paths": paths,
            "media_mount": True,
            "port_owned": port_owned,
            "port_ownership": port_ownership,
            "current_service_healthy": identity.get("health") == "healthy",
            "health": identity.get("health"),
            "current_mount_contract": current_mount_contract,
            "current_mounts": current_mount_contract,
            "candidate_mounts_verified": None,
            "candidate_mount_verification": "not_checked_pre_apply",
            "previous_immutable_repo_digest": previous_repo_digest is not None,
        },
        "secret_file_metadata": secret_metadata,
        "active_image": previous_repo_digest,
        "previous_immutable_repo_digest": previous_repo_digest,
        "image_id": identity.get("image_id"),
        "container_id": container,
        "port": port,
        "service": payload["service"],
        "project_directory": str(project),
        "_identity": identity,
    }


def atomic_bytes(path, data, mode):
    path = Path(path)
    if os.path.lexists(path) and stat.S_ISLNK(os.lstat(path).st_mode):
        raise RemoteFailure("refusing to replace a symlink")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def timestamp():
    return datetime_module.datetime.now(datetime_module.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def create_backup(payload, preflight):
    project, compose, env = project_paths(payload)
    root = project / ".asrsub-rollback"
    if os.path.lexists(root):
        if root.is_symlink() or not root.is_dir():
            raise RemoteFailure("rollback root is not a real directory")
    else:
        root.mkdir(mode=0o700)
    identity = preflight.get("_identity")
    if not isinstance(identity, dict):
        raise RemoteFailure("previous container identity is unavailable for rollback")
    previous_repo_digest = preflight.get("previous_immutable_repo_digest") or preflight.get("active_image")
    if not immutable(previous_repo_digest):
        raise RemoteFailure("previous image is not a recoverable immutable RepoDigest")
    previous_mount_contract = mount_contract(identity)
    if not previous_mount_contract:
        raise RemoteFailure("previous mount contract is unavailable")
    if len(previous_mount_contract) != len(identity.get("mounts", [])):
        raise RemoteFailure("previous mount contract is incomplete")
    backup_kind = "simple" if (
        identity.get("config_image") == previous_repo_digest and mounts_match(identity, payload)
    ) else "legacy"
    name = timestamp()
    backup = root / name
    suffix = 0
    while os.path.lexists(backup):
        suffix += 1
        backup = root / (name + "-" + str(suffix))
    backup.mkdir(mode=0o700)
    atomic_bytes(backup / payload["compose_file"], compose.read_bytes(), 0o600)
    atomic_bytes(backup / payload["env_file"], env.read_bytes(), 0o600)
    record = {
        "schema": "asrsub-simple-backup-v2",
        "verified": True,
        "created_at": name,
        "backup_kind": backup_kind,
        "previous_image": previous_repo_digest,
        "previous_repo_digest": previous_repo_digest,
        "previous_image_id": preflight.get("image_id"),
        "previous_mount_contract": previous_mount_contract,
        "compose_file": payload["compose_file"],
        "env_file": payload["env_file"],
        "webhook_port": preflight.get("port", payload["webhook_port"]),
    }
    atomic_bytes(backup / "metadata.json", (json.dumps(record, sort_keys=True) + "\n").encode(), 0o600)
    return backup, record


def restore_backup(payload, backup):
    project, compose, env = project_paths(payload)
    metadata_path = backup / "metadata.json"
    try:
        record = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RemoteFailure("rollback metadata is invalid") from exc
    if (
        not isinstance(record, dict)
        or record.get("verified") is not True
        or not immutable(record.get("previous_repo_digest", record.get("previous_image")))
        or record.get("backup_kind") not in {"legacy", "simple"}
        or not contract_tuples(record.get("previous_mount_contract"))
    ):
        raise RemoteFailure("rollback metadata is not a verified immutable backup contract")
    saved_compose = backup / payload["compose_file"]
    saved_env = backup / payload["env_file"]
    require_path(saved_compose, directory=False)
    require_path(saved_env, directory=False)
    atomic_bytes(compose, saved_compose.read_bytes(), 0o600)
    atomic_bytes(env, saved_env.read_bytes(), 0o600)
    return record


def latest_backup(payload):
    root = Path(payload["project_directory"]) / ".asrsub-rollback"
    if not os.path.lexists(root) or root.is_symlink() or not root.is_dir():
        raise RemoteFailure("rollback directory is unavailable or unsafe")
    candidates = []
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise RemoteFailure("rollback directory is unavailable") from exc
    for entry in entries:
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            value = json.loads((entry / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(value, dict)
            and value.get("verified") is True
            and value.get("backup_kind") in {"legacy", "simple"}
            and immutable(value.get("previous_repo_digest", value.get("previous_image")))
            and contract_tuples(value.get("previous_mount_contract"))
        ):
            created = value.get("created_at")
            if isinstance(created, str):
                candidates.append((created, entry.name, entry))
    if not candidates:
        raise RemoteFailure("no verified immutable rollback backup is available")
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def probe(port, path):
    try:
        request = urllib.request.Request("http://127.0.0.1:" + port + path, method="GET")
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False


def wait_ready(port, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = probe(port, "/health")
        ready = probe(port, "/ready")
        if health and ready:
            return {"health": True, "ready": True}
        time.sleep(1)
    raise RemoteFailure("bounded health/readiness gate failed")


def verify_rollback_runtime(payload, record, port):
    container, _row = active_container(payload)
    identity = inspect_container(container)
    digest = record.get("previous_repo_digest", record.get("previous_image"))
    if not immutable(digest) or digest not in identity.get("repo_digests", []):
        raise RemoteFailure("rollback does not prove the previous immutable RepoDigest")
    backup_kind = record.get("backup_kind")
    config_image = identity.get("config_image")
    if backup_kind == "simple":
        if config_image != digest:
            raise RemoteFailure("simple rollback does not prove the exact Config.Image digest")
        config_reference = "immutable_digest"
    elif backup_kind == "legacy":
        if not isinstance(config_image, str) or not config_image:
            raise RemoteFailure("legacy rollback has no Compose image reference")
        if immutable(config_image) and config_image != digest:
            raise RemoteFailure("legacy rollback Config.Image conflicts with the recorded digest")
        config_reference = "legacy/tag" if not immutable(config_image) else "legacy/digest"
    else:
        raise RemoteFailure("rollback metadata does not identify its backup contract")
    if identity.get("health") != "healthy":
        raise RemoteFailure("rolled back container is not healthy")
    if not mount_contract_matches(identity, record.get("previous_mount_contract")):
        raise RemoteFailure("rolled back container mounts do not match the saved previous contract")
    return {
        "container_id": container,
        "image_id": identity.get("image_id"),
        "image": digest,
        "health": identity.get("health"),
        "config_reference": config_reference,
        "repo_digest_matched": True,
        "mounts_verified": True,
        "port": port,
    }


def verify_image_and_runtime(payload, image, port):
    container, _row = active_container(payload)
    identity = inspect_container(container)
    if identity.get("config_image") != image or image not in identity.get("repo_digests", []):
        raise RemoteFailure("running container does not prove the exact requested digest")
    if identity.get("health") != "healthy":
        raise RemoteFailure("running container is not healthy")
    if not mounts_match(identity, payload):
        raise RemoteFailure("running container mounts are not the approved set")
    return {
        "container_id": container,
        "image_id": identity.get("image_id"),
        "image": image,
        "health": identity.get("health"),
        "candidate_mounts_verified": True,
        "mounts_verified": True,
        "port": port,
    }


def rollback_to(payload, backup):
    record = restore_backup(payload, backup)
    port = record.get("webhook_port", payload["webhook_port"])
    checked(compose_argv(payload, "up", "-d", "--no-build", "--pull=never", payload["service"]), "Compose rollback", cwd=Path(payload["project_directory"]))
    wait_ready(str(port))
    verification = verify_rollback_runtime(payload, record, str(port))
    return {"ok": True, "backup": backup.name, "image": record["previous_image"], "verification": verification}


def deploy(payload):
    expected = payload["expected_hostname"]
    actual = socket.gethostname()
    if actual != expected and socket.getfqdn() != expected:
        raise RemoteFailure("target hostname did not match before writes")
    preflight = collect(payload, strict=True)
    image = payload.get("image")
    if not immutable(image):
        raise RemoteFailure("candidate image is not the exact approved digest")
    backup, _record = create_backup(payload, preflight)
    try:
        project, compose, env = project_paths(payload)
        atomic_bytes(compose, base64.b64decode(payload["compose_text"]), 0o600)
        atomic_bytes(env, base64.b64decode(payload["env_text"]), 0o600)
        checked(compose_argv(payload, "config", "-q"), "Compose config", cwd=project)
        checked(compose_argv(payload, "pull", "--quiet", payload["service"]), "Compose digest pull", cwd=project)
        checked(compose_argv(payload, "up", "-d", "--no-build", "--pull=never", payload["service"]), "Compose apply", cwd=project)
        port = payload["webhook_port"]
        wait_ready(port)
        verification = verify_image_and_runtime(payload, image, port)
        return {"ok": True, "operation": "deploy", "backup": backup.name, "image": image, "verification": verification}
    except Exception:
        rollback_status = {"ok": False}
        try:
            rollback_status = rollback_to(payload, backup)
        except Exception:
            rollback_status = {"ok": False}
        return {"ok": False, "operation": "deploy", "error": "bounded deployment gate failed", "backup": backup.name, "rollback": rollback_status}


def rollback(payload):
    expected = payload["expected_hostname"]
    actual = socket.gethostname()
    if actual != expected and socket.getfqdn() != expected:
        raise RemoteFailure("target hostname did not match before writes")
    backup = latest_backup(payload)
    result = rollback_to(payload, backup)
    result["operation"] = "rollback"
    return result


def main(blob):
    try:
        payload = json.loads(base64.b64decode(blob).decode("utf-8"))
        operation = payload["operation"]
        if operation == "status":
            result = collect(payload, strict=False)
            result["operation"] = operation
        elif operation == "preflight":
            result = collect(payload, strict=True)
            result["operation"] = operation
        elif operation == "deploy":
            result = deploy(payload)
        elif operation == "rollback":
            result = rollback(payload)
        else:
            raise RemoteFailure("operation is not allowlisted")
        result.pop("_identity", None)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        raise SystemExit(0 if result.get("ok") is True else 2)
    except RemoteFailure:
        print(json.dumps({"ok": False, "error": "remote operation failed"}, separators=(",", ":")))
        raise SystemExit(2)
    except Exception:
        print(json.dumps({"ok": False, "error": "remote operation failed"}, separators=(",", ":")))
        raise SystemExit(2)
'''


if __name__ == "__main__":
    raise SystemExit(main())
