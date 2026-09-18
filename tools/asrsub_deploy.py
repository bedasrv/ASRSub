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
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


IMAGE_RE = re.compile(r"^ghcr\.io/bedasrv/asrsub@sha256:[0-9a-f]{64}$")
COMPOSE_IDENTIFIER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62})$")
TIMESTAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
BACKUP_SCHEMA = "asrsub-simple-backup-v2"
DEFAULT_TIMEOUT = 900.0
MAX_TIMEOUT = 3600.0
SSH_TRANSPORT_GRACE = 30.0
SSH_REAPER_TIMEOUT = SSH_TRANSPORT_GRACE
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
    r"(?:^|_)(?:ACCESS_KEY|ACCESS_TOKEN|API_KEY|AUTHORIZATION|BEARER|CERT|CERTIFICATE|COOKIE|CREDENTIALS?|ENCRYPTION_KEY|KEY|PASSWORD|PASSWD|PRIVATE_KEY|SECRET|TOKEN|WEBHOOK)(?:_|$)",
    re.IGNORECASE,
)
FORBIDDEN_COMMAND_TOKENS = frozenset(
    {"down", "prune", "rm", "restart", "systemctl", "daemon-reload"}
)
DROPPED_RESULT_KEYS = frozenset(
    {
        "stdout",
        "stderr",
        "secret",
        "secrets",
        "environment",
        "env",
        "raw",
        "raw_output",
        "raw_ss",
        "socket_listing",
        "pid",
        "pids",
        "process_pid",
    }
)


class DeployError(Exception):
    """A local validation, transport, or structured remote-operation error."""


class RecoveryRequired(DeployError):
    """The target has a pending operation that must be recovered first."""


def validate_compose_identifier(value: str, name: str) -> str:
    """Accept only a bounded Compose service/project identifier."""
    if not isinstance(value, str) or COMPOSE_IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase Compose identifier")
    return value


def _validate_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a finite positive number")
    value = float(value)
    if not math.isfinite(value) or value <= 0 or value > MAX_TIMEOUT:
        raise ValueError(f"timeout must be greater than zero and at most {MAX_TIMEOUT:g} seconds")
    return value


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        _datetime.datetime.strptime(value, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return False
    return True


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
    timeout: float = DEFAULT_TIMEOUT

    def __post_init__(self) -> None:
        validate_compose_identifier(self.service, "service")
        validate_compose_identifier(self.project_name, "project name")
        _validate_timeout(self.timeout)


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
    expected_pid: int | None = None,
) -> dict[str, Any]:
    """Return bounded ownership metadata for one LISTEN port, never raw ss output."""
    port = _validate_port(port)
    names: set[str] = set()
    process_pids: set[tuple[str, int]] = set()
    listening = False
    for line in socket_listing.splitlines():
        fields = line.split()
        if not fields or fields[0] != "LISTEN" or len(fields) < 4:
            continue
        local_endpoint = fields[3]
        if local_endpoint.rsplit(":", 1)[-1] != port:
            continue
        listening = True
        for name, raw_pid in re.findall(r'\(\("([^\"]+)",pid=([0-9]+)', line):
            names.add(name)
            process_pids.add((name, int(raw_pid)))
    ordered_names = sorted(names)
    if expected_pid is None:
        owned = expected_process in names
    else:
        owned = (expected_process, expected_pid) in process_pids
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
        "/usr/bin/docker",
        "--context",
        "default",
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
        if key in MANAGED_ENV_KEYS:
            continue
        if SENSITIVE_ENV_KEY.search(key):
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


def _reap_timed_out_ssh(process: subprocess.Popen[Any]) -> None:
    """Drain a timed-out SSH child for a bounded grace period without killing it."""
    try:
        # The remote script has a finite deadline.  Keep both pipes drained for
        # only the explicit transport grace period; never interrupt a remote
        # apply or rollback after the target may have started mutating files.
        process.communicate(timeout=SSH_REAPER_TIMEOUT)
    except subprocess.TimeoutExpired:
        # The child may still be completing a remote mutation.  Returning from
        # the daemon reaper is safer than killing it or blocking indefinitely.
        return
    except Exception:
        try:
            process.wait(timeout=SSH_REAPER_TIMEOUT)
        except Exception:
            pass


def stream_ssh(command: Sequence[str], script: str, timeout: float) -> SSHResult:
    """Stream SSH; timeout returns recovery-required while a bounded reaper drains it."""
    timeout = float(timeout)
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
        # The remote Python program owns the mutation and has its own deadline.
        # Do not kill SSH here: doing so can interrupt a remote apply or rollback
        # after the target has already replaced one of its two files.
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        threading.Thread(
            target=_reap_timed_out_ssh,
            args=(process,),
            name="asrsub-ssh-reaper",
            daemon=True,
        ).start()
        raise RecoveryRequired(
            "SSH transport deadline reached; remote operation may still be in progress; "
            "recover with status or rollback"
        ) from exc
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
        "timeout": config.timeout,
        **(payload or {}),
    }
    validate_compose_identifier(merged["service"], "service")
    validate_compose_identifier(merged["project_name"], "project name")
    _validate_timeout(merged["timeout"])
    result = runner(
        ssh_command(config.target),
        remote_program(operation, merged),
        config.timeout + SSH_TRANSPORT_GRACE,
    )
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


def _safe_backup_filename(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not os.path.isabs(value)
        and "/" not in value
        and "\\\\" not in value
        and value not in {".", ".."}
    )


def _valid_mount_contract(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for mount in value:
        if not isinstance(mount, dict):
            return False
        if not isinstance(mount.get("source"), str) or not isinstance(mount.get("destination"), str):
            return False
        if type(mount.get("rw")) is not bool:
            return False
    return True


def _valid_backup_record(path: Path, *, compose_file: str | None = None, env_file: str | None = None) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    try:
        record = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict):
        return False
    if (
        record.get("schema") != BACKUP_SCHEMA
        or record.get("verified") is not True
        or record.get("backup_kind") not in {"legacy", "simple"}
        or not _valid_timestamp(record.get("created_at"))
        or not isinstance(record.get("previous_image"), str)
        or IMAGE_RE.fullmatch(record["previous_image"]) is None
        or not isinstance(record.get("previous_repo_digest"), str)
        or IMAGE_RE.fullmatch(record["previous_repo_digest"]) is None
        or not _valid_mount_contract(record.get("previous_mount_contract"))
        or not _safe_backup_filename(record.get("compose_file"))
        or not _safe_backup_filename(record.get("env_file"))
        or record.get("compose_sha256") != record.get("compose_sha256", "").lower()
        or not isinstance(record.get("compose_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", record.get("compose_sha256", ""))
        or not isinstance(record.get("env_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", record.get("env_sha256", ""))
    ):
        return False
    if compose_file is not None and record["compose_file"] != compose_file:
        return False
    if env_file is not None and record["env_file"] != env_file:
        return False
    compose = path / record["compose_file"]
    env = path / record["env_file"]
    if (
        compose.is_symlink()
        or not compose.is_file()
        or env.is_symlink()
        or not env.is_file()
    ):
        return False
    try:
        return (
            hashlib.sha256(compose.read_bytes()).hexdigest() == record["compose_sha256"]
            and hashlib.sha256(env.read_bytes()).hexdigest() == record["env_sha256"]
        )
    except OSError:
        return False


def select_latest_verified_backup(backups: Iterable[Path]) -> Path:
    """Select the newest complete immutable backup and skip malformed candidates."""
    candidates: list[tuple[str, str, Path]] = []
    for raw_path in backups:
        path = Path(raw_path)
        if not _valid_backup_record(path):
            continue
        try:
            created = json.loads((path / "metadata.json").read_text(encoding="utf-8"))["created_at"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError):
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
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
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
            timeout=_validate_timeout(args.timeout),
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
import contextlib
import datetime as datetime_module
import fcntl
import hashlib
import json
import math
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
COMPOSE_IDENTIFIER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62})$")
TIMESTAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
BACKUP_SCHEMA = "asrsub-simple-backup-v2"
PENDING_SCHEMA = "asrsub-simple-pending-v1"
DEFAULT_TIMEOUT = 900.0
MAX_TIMEOUT = 3600.0
READINESS_TIMEOUT = 60.0
FORBIDDEN = frozenset({"down", "prune", "rm", "restart", "systemctl", "daemon-reload"})
EXPECTED_LISTENER_PROCESS = "asrsub"
MANAGED_ENV_KEYS = frozenset({"ASRSUB_IMAGE", "WEBHOOK_PORT", "NAS_MEDIA_PREFIX", "PROVIDER_KEYS_FILE"})
SENSITIVE_ENV_KEY = re.compile(
    r"(?:^|_)(?:ACCESS_KEY|ACCESS_TOKEN|API_KEY|AUTHORIZATION|BEARER|CERT|CERTIFICATE|COOKIE|CREDENTIALS?|ENCRYPTION_KEY|KEY|PASSWORD|PASSWD|PRIVATE_KEY|SECRET|TOKEN|WEBHOOK)(?:_|$)",
    re.IGNORECASE,
)
ACTIVE_DEADLINE = None
MAX_MEDIA_MOUNT_BYTES = 8192
MAX_MEDIA_MOUNT_LINES = 64
MAX_MEDIA_MOUNT_FIELD = 4096


class RemoteFailure(Exception):
    pass


class RecoveryRequired(RemoteFailure):
    pass


def immutable(value):
    return isinstance(value, str) and IMAGE_RE.fullmatch(value) is not None


def validate_identifier(value, label):
    if not isinstance(value, str) or COMPOSE_IDENTIFIER_RE.fullmatch(value) is None:
        raise RemoteFailure(label + " is not a safe Compose identifier")
    return value


def validate_timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RemoteFailure("operation timeout is invalid")
    value = float(value)
    if not math.isfinite(value) or value <= 0 or value > MAX_TIMEOUT:
        raise RemoteFailure("operation timeout is invalid")
    return value


def _remaining_timeout(timeout, deadline=None):
    active = deadline if deadline is not None else ACTIVE_DEADLINE
    requested = 60.0 if timeout is None else validate_timeout(timeout)
    if active is None:
        return requested
    remaining = active - time.monotonic()
    if remaining <= 0:
        raise RemoteFailure("operation deadline exceeded")
    return min(requested, remaining)


def minimal_environment():
    environment = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/home/user")}
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES"):
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    environment.setdefault("LC_ALL", "C")
    return environment


def checked(argv, label, *, cwd=None, timeout=None, deadline=None):
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
            timeout=_remaining_timeout(timeout, deadline),
            env=minimal_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RemoteFailure(label) from exc
    if result.returncode != 0:
        raise RemoteFailure(label)
    return result.stdout


def project_paths(payload):
    project = Path(payload["project_directory"])
    if not project.is_absolute():
        raise RemoteFailure("project directory is not a real directory")
    _check_parent_components(project)
    project_info = metadata(project)
    if not project_info.get("present") or project_info.get("symlink") or not project_info.get("directory"):
        raise RemoteFailure("project directory is not a real directory")
    compose_name = payload["compose_file"]
    env_name = payload["env_file"]
    if any(not isinstance(name, str) or not name or os.path.isabs(name) or "/" in name or "\\" in name or name in {".", ".."} for name in (compose_name, env_name)):
        raise RemoteFailure("compose and env paths must be single file names")
    compose = project / compose_name
    env = project / env_name
    return project, compose, env


def docker_argv(*args):
    return ["/usr/bin/docker", "--context", "default", *[str(item) for item in args]]


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


def require_path(path, *, directory=None, role=None):
    path = Path(path)
    _check_parent_components(path)
    info = metadata(path)
    if not info.get("present") or info.get("symlink"):
        raise RemoteFailure("required path is absent or unsafe")
    if directory is True and not info.get("directory"):
        raise RemoteFailure("required directory is not a directory")
    if directory is False and not info.get("regular"):
        raise RemoteFailure("required file is not regular")
    _validate_role(path, info, directory=directory, role=role)
    return info


def validate_non_secret_env(text):
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RemoteFailure("non-secret env file is malformed")
        key, _value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise RemoteFailure("non-secret env file is malformed")
        if key in MANAGED_ENV_KEYS:
            continue
        if SENSITIVE_ENV_KEY.search(key):
            raise RemoteFailure("active env file contains a secret-bearing key")


def validate_env_file(path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RemoteFailure("non-secret env file cannot be read") from exc
    validate_non_secret_env(text)
    return text


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
    validate_identifier(payload.get("service"), "service")
    validate_identifier(payload.get("project_name"), "project name")
    allowed = {"config", "pull", "up", "ps"}
    if not operation or operation[0] not in allowed or any(item in FORBIDDEN for item in operation):
        raise RemoteFailure("Compose operation is not allowlisted")
    return docker_argv(
        "compose",
        "--project-directory", str(project),
        "--env-file", str(env),
        "-f", str(compose),
        "-p", payload["project_name"],
        *operation,
    )


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
        output = checked(docker_argv("inspect", "--format", expression, container), label)
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
    pid = inspect("{{json .State.Pid}}", "container pid")
    if type(pid) is not int or pid <= 0:
        raise RemoteFailure("active container has no valid host pid")
    return {
        "labels": labels if isinstance(labels, dict) else {},
        "mounts": mounts if isinstance(mounts, list) else [],
        "health": health,
        "config_image": config_image,
        "image_id": image_id,
        "repo_digests": repo_digests if isinstance(repo_digests, list) else [],
        "pid": pid,
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


def parse_port_ownership(socket_listing, port, expected_process=EXPECTED_LISTENER_PROCESS, expected_pid=None):
    names = set()
    process_pids = set()
    listening = False
    for line in socket_listing.splitlines():
        fields = line.split()
        if not fields or fields[0] != "LISTEN" or len(fields) < 4:
            continue
        if fields[3].rsplit(":", 1)[-1] != port:
            continue
        listening = True
        names.update(re.findall(r'\(\("([^"]+)"', line))
        for name, raw_pid in re.findall(r'\(\("([^"]+)",pid=([0-9]+)', line):
            process_pids.add((name, int(raw_pid)))
    ordered_names = sorted(names)
    if expected_pid is None:
        owned = expected_process in names
    else:
        owned = (expected_process, expected_pid) in process_pids
    return {
        "port": port,
        "status": "owned" if owned else "unrelated" if listening else "not_listening",
        "process_name": expected_process if owned else (ordered_names[0] if ordered_names else None),
        "process_names": ordered_names,
    }


def parse_media_mount(findmnt_output, requested_child):
    if not isinstance(findmnt_output, str) or not isinstance(requested_child, str):
        raise RemoteFailure("media mount metadata is invalid")
    if len(findmnt_output) > MAX_MEDIA_MOUNT_BYTES:
        raise RemoteFailure("media mount metadata is too large")
    requested = Path(requested_child)
    if not requested.is_absolute() or any(part in {".", ".."} for part in requested.parts):
        raise RemoteFailure("media mount child is invalid")
    for line in findmnt_output.splitlines()[:MAX_MEDIA_MOUNT_LINES]:
        if len(line) > MAX_MEDIA_MOUNT_FIELD:
            raise RemoteFailure("media mount metadata is too large")
        fields = line.split(None, 2)
        if len(fields) != 3:
            continue
        target, fstype, source = fields
        if (
            not target.startswith("/")
            or len(target) > MAX_MEDIA_MOUNT_FIELD
            or len(fstype) > MAX_MEDIA_MOUNT_FIELD
            or len(source) > MAX_MEDIA_MOUNT_FIELD
            or not source.strip()
            or fstype not in {"nfs", "nfs4"}
        ):
            continue
        target_path = Path(target)
        if not target_path.is_absolute() or any(part in {".", ".."} for part in target_path.parts):
            continue
        try:
            requested.relative_to(target_path)
        except ValueError:
            continue
        return {
            "target": str(target_path),
            "fstype": fstype,
            "source": source.strip(),
        }
    raise RemoteFailure("media path is not on an approved NFS mount")


def _check_parent_components(path):
    path = Path(path)
    if not path.is_absolute():
        raise RemoteFailure("managed path must be absolute")
    current = Path(path.anchor)
    for component in path.parts[1:-1]:
        current /= component
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise RemoteFailure("managed path parent is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RemoteFailure("managed path has an unsafe parent")


def _validate_role(path, info, *, directory, role):
    if role is None:
        return
    if role not in {"project", "project_file", "rollback"}:
        raise RemoteFailure("managed path role is invalid")
    if info.get("uid") != os.geteuid():
        raise RemoteFailure("managed path owner is not the deployment user")
    mode = info.get("mode", 0)
    if role == "project":
        if directory is not True or mode & 0o022 or mode & 0o700 != 0o700:
            raise RemoteFailure("project directory permissions are unsafe")
    elif role == "project_file":
        if directory is not False or mode & 0o022 or mode & 0o400 != 0o400:
            raise RemoteFailure("project file permissions are unsafe")
    elif role == "rollback":
        if directory is not True or mode != 0o700:
            raise RemoteFailure("rollback directory permissions are unsafe")


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
        rw = mount.get("RW")
        if not isinstance(source, str) or not isinstance(destination, str) or type(rw) is not bool:
            raise RemoteFailure("container mount metadata is invalid")
        contract.append({"source": source, "destination": destination, "rw": rw})
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
        rw = mount.get("rw")
        if not isinstance(source, str) or not isinstance(destination, str) or type(rw) is not bool:
            return []
        values.append((source, destination, rw))
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
        "project": require_path(project, directory=True, role="project"),
        "compose": require_path(compose, directory=False, role="project_file"),
        "env": require_path(env, directory=False, role="project_file"),
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
    checked(docker_argv("version", "--format", "{{.Server.Version}}"), "Docker version")
    checked(docker_argv("compose", "version", "--short"), "Compose version")
    media_mount = parse_media_mount(
        checked(
            ["findmnt", "-T", "/mnt/nas/share/media", "-n", "-o", "TARGET,FSTYPE,SOURCE"],
            "media mount",
        ),
        "/mnt/nas/share/media",
    )
    port = env_values(env).get("WEBHOOK_PORT", payload["webhook_port"])
    if not re.fullmatch(r"[1-9][0-9]{0,4}", port or "") or int(port) > 65535:
        raise RemoteFailure("WEBHOOK_PORT is invalid")
    container, row = active_container(payload)
    identity = inspect_container(container)
    socket_listing = checked(["ss", "-ltnp"], "port ownership")
    port_ownership = parse_port_ownership(socket_listing, port, expected_pid=identity.get("pid"))
    port_owned = port_ownership["status"] == "owned"
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
            "media_mount": {
                "target": media_mount["target"],
                "fstype": media_mount["fstype"],
            },
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


def verify_port_ownership(port, identity):
    expected_pid = identity.get("pid") if isinstance(identity, dict) else None
    if expected_pid is None:
        return None
    if type(expected_pid) is not int or expected_pid <= 0:
        raise RemoteFailure("active container pid is invalid")
    socket_listing = checked(["ss", "-ltnp"], "post-apply port ownership")
    ownership = parse_port_ownership(socket_listing, port, expected_pid=expected_pid)
    if ownership["status"] != "owned":
        raise RemoteFailure("configured port is not owned by the active ASRSub listener")
    return ownership


def _open_parent_directory(path):
    path = Path(path)
    if not path.is_absolute() or not path.name or any(part in {".", ".."} for part in path.parts):
        raise RemoteFailure("write path is invalid")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory_flag:
        raise RemoteFailure("safe directory opens are unavailable")
    flags = os.O_RDONLY | directory_flag | nofollow
    current_fd = None
    try:
        current_fd = os.open(path.anchor, flags)
        for component in path.parent.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError as exc:
        if current_fd is not None:
            os.close(current_fd)
        raise RemoteFailure("write path has an unsafe parent") from exc


def atomic_bytes(path, data, mode):
    path = Path(path)
    parent_fd = _open_parent_directory(path)
    temporary = None
    fd = None
    try:
        try:
            existing = os.lstat(path.name, dir_fd=parent_fd)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise RemoteFailure("refusing to replace a symlink")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        for _attempt in range(16):
            candidate = "." + path.name + "." + str(os.getpid()) + "." + os.urandom(8).hex()
            try:
                fd = os.open(candidate, flags, mode, dir_fd=parent_fd)
            except FileExistsError:
                continue
            temporary = candidate
            break
        if fd is None or temporary is None:
            raise RemoteFailure("could not allocate a temporary file")
        handle = os.fdopen(fd, "wb")
        fd = None
        try:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary = None
        os.fsync(parent_fd)
    except RemoteFailure:
        raise
    except OSError as exc:
        raise RemoteFailure("atomic write failed") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def timestamp():
    return datetime_module.datetime.now(datetime_module.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def valid_timestamp(value):
    if not isinstance(value, str) or TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        datetime_module.datetime.strptime(value, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return False
    return True


def safe_backup_filename(value):
    return (
        isinstance(value, str)
        and bool(value)
        and not os.path.isabs(value)
        and "/" not in value
        and "\\\\" not in value
        and value not in {".", ".."}
    )


def valid_contract(value):
    return bool(contract_tuples(value))


def backup_root(payload):
    project = Path(payload["project_directory"])
    if not project.is_absolute():
        raise RemoteFailure("rollback project directory is unsafe")
    _check_parent_components(project)
    project_info = metadata(project)
    if not project_info.get("present") or project_info.get("symlink") or not project_info.get("directory"):
        raise RemoteFailure("rollback project directory is unsafe")
    root = project / ".asrsub-rollback"
    if not os.path.lexists(root):
        raise RemoteFailure("rollback directory is unavailable or unsafe")
    require_path(root, directory=True, role="rollback")
    return root


def read_backup_record(payload, backup):
    backup = Path(backup)
    require_path(backup, directory=True, role="rollback")
    metadata_path = backup / "metadata.json"
    require_path(metadata_path, directory=False, role="project_file")
    try:
        record = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RemoteFailure("rollback metadata is invalid") from exc
    compose_name = payload.get("compose_file", "compose.yaml")
    env_name = payload.get("env_file", ".env")
    if (
        not isinstance(record, dict)
        or record.get("schema") != BACKUP_SCHEMA
        or record.get("verified") is not True
        or record.get("backup_kind") not in {"legacy", "simple"}
        or not valid_timestamp(record.get("created_at"))
        or not immutable(record.get("previous_image"))
        or not immutable(record.get("previous_repo_digest"))
        or record.get("compose_file") != compose_name
        or record.get("env_file") != env_name
        or not safe_backup_filename(record.get("compose_file"))
        or not safe_backup_filename(record.get("env_file"))
        or not re.fullmatch(r"[0-9a-f]{64}", record.get("compose_sha256", ""))
        or not re.fullmatch(r"[0-9a-f]{64}", record.get("env_sha256", ""))
        or not valid_contract(record.get("previous_mount_contract"))
    ):
        raise RemoteFailure("rollback metadata is not a complete immutable backup contract")
    saved_compose = backup / record["compose_file"]
    saved_env = backup / record["env_file"]
    require_path(saved_compose, directory=False, role="project_file")
    require_path(saved_env, directory=False, role="project_file")
    try:
        compose_bytes = saved_compose.read_bytes()
        env_bytes = saved_env.read_bytes()
        validate_non_secret_env(env_bytes.decode("utf-8"))
    except (OSError, UnicodeError) as exc:
        raise RemoteFailure("rollback files are not valid non-secret files") from exc
    if (
        hashlib.sha256(compose_bytes).hexdigest() != record["compose_sha256"]
        or hashlib.sha256(env_bytes).hexdigest() != record["env_sha256"]
    ):
        raise RemoteFailure("rollback file hash does not match metadata")
    return record


@contextlib.contextmanager
def mutation_lock(payload, timeout=None):
    project, _compose, _env = project_paths(payload)
    lock_path = project / ".asrsub-deploy.lock"
    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise OSError("safe lock open is unavailable")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | nofollow, 0o600)
    except OSError as exc:
        raise RemoteFailure("mutating deployment lock is unavailable") from exc
    deadline = ACTIVE_DEADLINE
    if deadline is None:
        budget = 60.0 if timeout is None else validate_timeout(timeout)
        deadline = time.monotonic() + budget
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RemoteFailure("another mutating deployment is in progress")
                time.sleep(min(0.05, remaining))
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        else:
            os.close(fd)


def pending_marker_path(payload):
    project = Path(payload["project_directory"])
    if not project.is_absolute():
        raise RemoteFailure("project directory is not a real directory")
    _check_parent_components(project)
    project_info = metadata(project)
    if not project_info.get("present") or project_info.get("symlink") or not project_info.get("directory"):
        raise RemoteFailure("project directory is not a real directory")
    return project / ".asrsub-pending.json"


def _load_pending(payload):
    marker_path = pending_marker_path(payload)
    if not os.path.lexists(marker_path):
        return None
    if marker_path.is_symlink() or not marker_path.is_file():
        raise RecoveryRequired("pending operation marker is unsafe; manual recovery is required")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryRequired("pending operation marker is invalid; manual recovery is required") from exc
    if (
        not isinstance(marker, dict)
        or marker.get("schema") != PENDING_SCHEMA
        or marker.get("operation") not in {"deploy", "rollback"}
        or not safe_backup_filename(marker.get("backup"))
        or marker.get("compose_file") != payload.get("compose_file", "compose.yaml")
        or marker.get("env_file") != payload.get("env_file", ".env")
        or not valid_timestamp(marker.get("created_at"))
    ):
        raise RecoveryRequired("pending operation marker is invalid; manual recovery is required")
    if marker.get("candidate_image") is not None and not immutable(marker["candidate_image"]):
        raise RecoveryRequired("pending operation marker has an invalid candidate image")
    root = backup_root(payload)
    backup = root / marker["backup"]
    try:
        read_backup_record(payload, backup)
    except RemoteFailure as exc:
        raise RecoveryRequired("pending backup is invalid; manual recovery is required") from exc
    return marker, backup


def ensure_no_pending(payload):
    if _load_pending(payload) is not None:
        raise RecoveryRequired("pending operation requires rollback recovery")


def write_pending(payload, backup, operation, *, candidate_image=None):
    if operation not in {"deploy", "rollback"}:
        raise RemoteFailure("pending operation is not allowlisted")
    record = read_backup_record(payload, backup)
    if candidate_image is not None and not immutable(candidate_image):
        raise RemoteFailure("pending candidate image is not immutable")
    marker = {
        "schema": PENDING_SCHEMA,
        "operation": operation,
        "created_at": timestamp(),
        "backup": Path(backup).name,
        "compose_file": record["compose_file"],
        "env_file": record["env_file"],
        "previous_image": record["previous_image"],
        "previous_repo_digest": record["previous_repo_digest"],
    }
    if candidate_image is not None:
        marker["candidate_image"] = candidate_image
    atomic_bytes(pending_marker_path(payload), (json.dumps(marker, sort_keys=True) + "\n").encode(), 0o600)


def clear_pending(payload):
    marker_path = pending_marker_path(payload)
    if not os.path.lexists(marker_path):
        return
    if marker_path.is_symlink() or not marker_path.is_file():
        raise RemoteFailure("pending operation marker is unsafe")
    os.unlink(marker_path)
    directory_fd = os.open(marker_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def create_backup(payload, preflight):
    project, compose, env = project_paths(payload)
    root = project / ".asrsub-rollback"
    if os.path.lexists(root):
        require_path(root, directory=True, role="rollback")
    else:
        try:
            root.mkdir(mode=0o700)
        except OSError as exc:
            raise RemoteFailure("rollback root could not be created") from exc
        require_path(root, directory=True, role="rollback")
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
    try:
        compose_bytes = compose.read_bytes()
        env_bytes = env.read_bytes()
        validate_non_secret_env(env_bytes.decode("utf-8"))
    except (OSError, UnicodeError) as exc:
        raise RemoteFailure("active files are not valid non-secret rollback inputs") from exc
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
    require_path(backup, directory=True, role="rollback")
    atomic_bytes(backup / payload["compose_file"], compose_bytes, 0o600)
    atomic_bytes(backup / payload["env_file"], env_bytes, 0o600)
    record = {
        "schema": BACKUP_SCHEMA,
        "verified": True,
        "created_at": name,
        "backup_kind": backup_kind,
        "previous_image": previous_repo_digest,
        "previous_repo_digest": previous_repo_digest,
        "previous_image_id": preflight.get("image_id"),
        "previous_mount_contract": previous_mount_contract,
        "compose_file": payload["compose_file"],
        "env_file": payload["env_file"],
        "compose_sha256": hashlib.sha256(compose_bytes).hexdigest(),
        "env_sha256": hashlib.sha256(env_bytes).hexdigest(),
        "webhook_port": preflight.get("port", payload.get("webhook_port", "8085")),
    }
    atomic_bytes(backup / "metadata.json", (json.dumps(record, sort_keys=True) + "\n").encode(), 0o600)
    read_backup_record(payload, backup)
    return backup, record


def restore_backup(payload, backup):
    project, compose, env = project_paths(payload)
    record = read_backup_record(payload, backup)
    saved_compose = Path(backup) / record["compose_file"]
    saved_env = Path(backup) / record["env_file"]
    atomic_bytes(compose, saved_compose.read_bytes(), 0o600)
    atomic_bytes(env, saved_env.read_bytes(), 0o600)
    return record


def latest_backup(payload):
    pending = _load_pending(payload)
    if pending is not None:
        return pending[1]
    root = backup_root(payload)
    candidates = []
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise RemoteFailure("rollback directory is unavailable") from exc
    for entry in entries:
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            value = read_backup_record(payload, entry)
        except RemoteFailure:
            continue
        candidates.append((value["created_at"], entry.name, entry))
    if not candidates:
        raise RemoteFailure("no verified immutable rollback backup is available")
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def probe(port, path, *, deadline=None):
    try:
        remaining = _remaining_timeout(3.0, deadline)
        request = urllib.request.Request("http://127.0.0.1:" + port + path, method="GET")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=remaining) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, RemoteFailure):
        return False


def wait_ready(port, timeout=READINESS_TIMEOUT, deadline=None):
    phase_deadline = time.monotonic() + validate_timeout(timeout)
    active = deadline if deadline is not None else ACTIVE_DEADLINE
    if active is not None:
        phase_deadline = min(phase_deadline, active)
    while True:
        remaining = phase_deadline - time.monotonic()
        if remaining <= 0:
            raise RemoteFailure("bounded health/readiness gate failed")
        health = probe(port, "/health", deadline=phase_deadline)
        ready = probe(port, "/ready", deadline=phase_deadline)
        if health and ready:
            return {"health": True, "ready": True}
        time.sleep(min(1.0, max(0.001, phase_deadline - time.monotonic())))


def verify_rollback_runtime(payload, record, port):
    container, _row = active_container(payload)
    identity = inspect_container(container)
    digest = record.get("previous_repo_digest")
    if (
        not immutable(record.get("previous_image"))
        or not immutable(digest)
        or not immutable(digest)
        or digest not in identity.get("repo_digests", [])
    ):
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
    verify_port_ownership(str(port), identity)
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
    verify_port_ownership(str(port), identity)
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
    port = record.get("webhook_port", payload.get("webhook_port", "8085"))
    if not isinstance(port, str) or not re.fullmatch(r"[1-9][0-9]{0,4}", port) or int(port) > 65535:
        raise RemoteFailure("rollback readiness port is invalid")
    checked(
        compose_argv(payload, "up", "-d", "--no-build", "--pull=never", payload["service"]),
        "Compose rollback",
        cwd=Path(payload["project_directory"]),
    )
    wait_ready(str(port))
    verification = verify_rollback_runtime(payload, record, str(port))
    clear_pending(payload)
    return {"ok": True, "backup": backup.name, "image": record.get("previous_image"), "verification": verification}


def _pending_recovery_required(payload):
    try:
        return _load_pending(payload) is not None
    except RecoveryRequired:
        return True


def _deploy_locked(payload):
    ensure_no_pending(payload)
    expected = payload["expected_hostname"]
    actual = socket.gethostname()
    if actual != expected and socket.getfqdn() != expected:
        raise RemoteFailure("target hostname did not match before writes")
    preflight = collect(payload, strict=True)
    image = payload.get("image")
    if not immutable(image):
        raise RemoteFailure("candidate image is not the exact approved digest")
    backup, _record = create_backup(payload, preflight)
    write_pending(payload, backup, "deploy", candidate_image=image)
    try:
        project, compose, env = project_paths(payload)
        atomic_bytes(compose, base64.b64decode(payload["compose_text"]), 0o600)
        atomic_bytes(env, base64.b64decode(payload["env_text"]), 0o600)
        validate_non_secret_env(env.read_text(encoding="utf-8"))
        checked(compose_argv(payload, "config", "-q"), "Compose config", cwd=project)
        checked(compose_argv(payload, "pull", "--quiet", payload["service"]), "Compose digest pull", cwd=project)
        checked(compose_argv(payload, "up", "-d", "--no-build", "--pull=never", payload["service"]), "Compose apply", cwd=project)
        port = payload["webhook_port"]
        wait_ready(port)
        verification = verify_image_and_runtime(payload, image, port)
        clear_pending(payload)
        return {"ok": True, "operation": "deploy", "backup": backup.name, "image": image, "verification": verification}
    except Exception:
        rollback_status = {"ok": False}
        try:
            rollback_status = rollback_to(payload, backup)
        except Exception:
            rollback_status = {"ok": False}
        recovery_required = _pending_recovery_required(payload)
        result = {
            "ok": False,
            "operation": "deploy",
            "error": "bounded deployment gate failed",
            "backup": backup.name,
            "rollback": rollback_status,
        }
        if recovery_required:
            result["recovery_required"] = True
            result["error"] = "deployment failed; pending rollback recovery is required"
        return result


def deploy(payload):
    with mutation_lock(payload):
        return _deploy_locked(payload)


def rollback(payload):
    with mutation_lock(payload):
        expected = payload["expected_hostname"]
        actual = socket.gethostname()
        if actual != expected and socket.getfqdn() != expected:
            raise RemoteFailure("target hostname did not match before writes")
        pending = _load_pending(payload)
        backup = latest_backup(payload)
        if pending is None:
            write_pending(payload, backup, "rollback")
        result = rollback_to(payload, backup)
        result["operation"] = "rollback"
        return result


def main(blob):
    global ACTIVE_DEADLINE
    try:
        payload = json.loads(base64.b64decode(blob).decode("utf-8"))
        timeout = validate_timeout(payload.get("timeout"))
        ACTIVE_DEADLINE = time.monotonic() + timeout
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
    except RecoveryRequired:
        print(json.dumps({"ok": False, "error": "pending operation requires manual rollback recovery", "recovery_required": True}, separators=(",", ":")))
        raise SystemExit(2)
    except RemoteFailure:
        print(json.dumps({"ok": False, "error": "remote operation failed"}, separators=(",", ":")))
        raise SystemExit(2)
    except Exception:
        print(json.dumps({"ok": False, "error": "remote operation failed"}, separators=(",", ":")))
        raise SystemExit(2)
'''


if __name__ == "__main__":
    raise SystemExit(main())
