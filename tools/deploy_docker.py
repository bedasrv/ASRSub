#!/usr/bin/env python3
"""Fail-closed Docker adapter for the immutable ASRSub deployment contract."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from production_adapter_common import (
    AdapterError,
    atomic_write_json,
    default_owner,
    ensure_no_symlink,
    ensure_parent_directory,
    environment_secret_values,
    production_command_environment,
    read_json,
    redact_text,
    reject_ambient_docker_environment,
    require_absolute,
    require_hex,
    require_image_digest,
    run_argv,
    sha256_file,
)


DOCKER = Path("/usr/bin/docker")
PROJECT_DIRECTORY = Path("/opt/mediastack/asrsub")
COMPOSE_FILE = PROJECT_DIRECTORY / "compose.yaml"
DEPLOY_EVIDENCE_ROOT = Path("/var/lib/asrsub/deploy-state/evidence")
APPROVED_IMAGE_PATH = Path("/var/lib/asrsub/deploy-state/approved-image.json")
IMAGE_INSPECT_EVIDENCE_PATH = DEPLOY_EVIDENCE_ROOT / "image-inspect.json"

_OPERATION_ALIASES = {
    "digest": "image-inspect",
    "preflight": "image-inspect",
    "config": "compose-config",
    "pull": "image-pull",
    "image_pull": "image-pull",
    "up": "compose-up",
    "compose_up": "compose-up",
    "start": "compose-start",
    "compose_start": "compose-start",
    "stop": "compose-stop",
    "compose_stop": "compose-stop",
    "ps": "compose-ps",
    "compose_ps": "compose-ps",
    "compose_config": "compose-config",
}
_OPERATIONS = {
    "image-inspect",
    "image-pull",
    "compose-config",
    "compose-up",
    "compose-start",
    "compose-stop",
    "compose-ps",
}
_COMPOSE_OPERATIONS = _OPERATIONS - {"image-inspect", "image-pull"}


def canonical_operation(value: str) -> str:
    operation = _OPERATION_ALIASES.get(value, value)
    if operation not in _OPERATIONS:
        allowed = ", ".join(sorted(_OPERATIONS))
        raise AdapterError(f"operation is not allowlisted: {value}; allowed: {allowed}")
    return operation


def _validate_executable(executable: Path, *, production: bool) -> Path:
    executable = require_absolute(executable, name="Docker executable")
    if production and executable != DOCKER:
        raise AdapterError("custom Docker executable is allowed only in the explicit test seam")
    if not production:
        ensure_no_symlink(executable, name="test Docker executable", allow_missing=False)
        if not executable.is_file():
            raise AdapterError("test Docker executable is missing")
    return executable


def _validate_compose_file(compose_file: Path | None, *, production: bool) -> Path:
    if compose_file is None:
        raise AdapterError("Compose operation requires --compose-file")
    compose_file = require_absolute(compose_file, name="compose file")
    ensure_no_symlink(compose_file, name="compose file", allow_missing=False)
    if production and compose_file != COMPOSE_FILE:
        raise AdapterError("production Compose file must be the fixed rendered path")
    if not compose_file.is_file():
        raise AdapterError(f"compose file is missing: {compose_file}")
    if compose_file.name in {".env", "docker-compose.override.yml", "docker-compose.override.yaml"}:
        raise AdapterError("override and .env Compose inputs are not allowed")
    return compose_file


def docker_argv(
    operation: str,
    *,
    digest: str | None = None,
    compose_file: Path | None = None,
    executable: Path = DOCKER,
    production: bool = False,
    test_seam: bool = False,
) -> list[str]:
    operation = canonical_operation(operation)
    if production and test_seam:
        raise AdapterError("production and test-seam modes are mutually exclusive")
    if production:
        _validate_executable(executable, production=True)
    elif test_seam:
        _validate_executable(executable, production=False)
    else:
        raise AdapterError("Docker argv requires an explicit production or test seam")
    command = [os.fspath(executable), "--context", "default"]
    if operation == "image-inspect":
        if digest is None:
            raise AdapterError("image-inspect requires --digest")
        require_image_digest(digest)
        return command + ["image", "inspect", "--format", "{{json .}}", digest]
    if operation == "image-pull":
        if digest is None:
            raise AdapterError("image-pull requires --digest")
        require_image_digest(digest)
        return command + ["image", "pull", "--quiet", digest]

    checked_compose = _validate_compose_file(compose_file, production=production)
    if test_seam:
        compose = ["compose", "-f", os.fspath(checked_compose)]
    else:
        # These flags are intentionally literal.  They prevent Compose from
        # consulting .env, an ambient project name, or interpolation sources.
        compose = [
            "compose",
            "--env-file",
            "/dev/null",
            "--project-directory",
            os.fspath(PROJECT_DIRECTORY),
            "-f",
            os.fspath(COMPOSE_FILE),
        ]
    if operation == "compose-config":
        return command + compose + ["config", "--no-interpolate", "--no-env-resolution", "--quiet"]
    if operation == "compose-up":
        return command + compose + ["up", "-d", "--no-build", "--pull=never"]
    if operation == "compose-start":
        return command + compose + ["start"]
    if operation == "compose-stop":
        return command + compose + ["stop"]
    return command + compose + ["ps", "--format", "{{json .}}"]


def _json_from_inspect(stdout: str) -> Any:
    try:
        value = json.loads(stdout.strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("Docker image inspect returned non-JSON output") from exc
    if isinstance(value, list):
        if len(value) != 1:
            raise AdapterError("Docker image inspect returned an ambiguous result")
        value = value[0]
    if not isinstance(value, dict):
        raise AdapterError("Docker image inspect returned an invalid object")
    return value


def _digest_in_observed(observed: dict[str, Any], digest: str) -> bool:
    values: list[str] = []
    for key in ("image_ref", "image_digest", "Id", "id"):
        value = observed.get(key)
        if isinstance(value, str):
            values.append(value)
    for key in ("RepoDigests", "repo_digests", "repoDigests"):
        value = observed.get(key)
        if isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
    return digest in values or digest.rsplit("@", 1)[-1] in values


def _filtered_identity(observed: dict[str, Any], digest: str) -> dict[str, Any]:
    filtered: dict[str, Any] = {}
    for key in ("image_ref", "image_digest", "Id", "id"):
        value = observed.get(key)
        if isinstance(value, str) and (value == digest or value == digest.rsplit("@", 1)[-1] or value == "sha256:" + digest.rsplit(":", 1)[-1]):
            filtered[key] = value
    for key in ("RepoDigests", "repo_digests", "repoDigests"):
        value = observed.get(key)
        if isinstance(value, list):
            matches = [item for item in value if isinstance(item, str) and (item == digest or item == digest.rsplit("@", 1)[-1])]
            if matches:
                filtered[key] = matches
    if not _digest_in_observed(filtered, digest):
        raise AdapterError("Docker image inspect did not prove the requested digest")
    return filtered


def _write_evidence(output: Path, value: dict[str, Any]) -> None:
    output = require_absolute(output, name="Docker evidence output")
    ensure_parent_directory(output, name="Docker evidence output")
    uid, gid = default_owner()
    atomic_write_json(output, value, mode=0o600, uid=uid, gid=gid, name="Docker evidence output")


def _read_approved_image(path: Path, expected_digest: str, *, release_sha: str | None = None) -> dict[str, Any]:
    value = read_json(require_absolute(path, name="approved image"), name="approved image")
    if not isinstance(value, dict) or value.get("schema") != "approved-image-v1":
        raise AdapterError("approved image has an unsupported schema")
    if value.get("image_ref") != expected_digest:
        raise AdapterError("approved image does not match the requested digest")
    expected_bare = expected_digest.rsplit(":", 1)[-1]
    if value.get("image_digest") != expected_bare:
        raise AdapterError("approved image digest is not bound to the image reference")
    if value.get("platform") != "linux/amd64":
        raise AdapterError("approved image platform is not approved")
    if release_sha is not None and value.get("release_sha") != release_sha:
        raise AdapterError("approved image release SHA does not match")
    return {"schema": value["schema"], "image_ref": value["image_ref"], "image_digest": value["image_digest"], "platform": value["platform"]}


def _read_image_evidence(path: Path, *, digest: str) -> None:
    value = read_json(require_absolute(path, name="image inspect evidence"), name="image inspect evidence")
    if not isinstance(value, dict) or value.get("schema") != "docker-operation-evidence-v1":
        raise AdapterError("image inspect evidence has an unsupported schema")
    if value.get("operation") != "image-inspect" or value.get("returncode") != 0:
        raise AdapterError("image inspect evidence is not a successful inspect")
    if value.get("requested_digest") != digest or value.get("image_digest") != digest:
        raise AdapterError("image inspect evidence digest is not bound")
    argv = value.get("argv")
    expected = docker_argv("image-inspect", digest=digest, production=True)
    if argv != expected:
        raise AdapterError("image inspect evidence command is not the fixed command")
    observed = value.get("observed")
    if not isinstance(observed, dict) or not _digest_in_observed(observed, digest):
        raise AdapterError("image inspect evidence does not prove the approved digest")


def _validate_rendered_compose(path: Path, expected_hash: str) -> str:
    expected_hash = require_hex(expected_hash, name="rendered Compose SHA256", length=64)
    actual = sha256_file(path, name="rendered Compose")
    if actual != expected_hash:
        raise AdapterError("rendered Compose hash does not match the approved transaction")
    return actual


def run_adapter(
    operation: str,
    *,
    digest: str | None,
    compose_file: Path | None,
    output: Path,
    executable: Path = DOCKER,
    test_seam: bool = False,
    compose_sha256: str | None = None,
    approved_image: Path | None = None,
    image_evidence: Path | None = None,
    release_sha: str | None = None,
) -> dict[str, Any]:
    operation = canonical_operation(operation)
    production = not test_seam
    source_env = dict(os.environ)
    reject_ambient_docker_environment(production=production, env=source_env)
    secret_values = environment_secret_values(source_env)
    output = require_absolute(output, name="Docker evidence output")
    if production and output.parent != DEPLOY_EVIDENCE_ROOT:
        raise AdapterError("production Docker evidence output must use the fixed evidence root")
    if operation in {"image-inspect", "image-pull"}:
        if digest is None:
            raise AdapterError(f"{operation} requires --digest")
        require_image_digest(digest)
    elif production and operation in {"compose-up", "compose-start", "compose-stop", "compose-ps"}:
        if digest is None:
            raise AdapterError(f"{operation} requires --digest")
        require_image_digest(digest)
    if operation in _COMPOSE_OPERATIONS:
        checked_compose = _validate_compose_file(compose_file, production=production)
    if production and operation == "compose-up":
        if compose_sha256 is None:
            raise AdapterError("compose-up requires the approved rendered Compose hash")
        if approved_image is None:
            raise AdapterError("compose-up requires the approved image identity")
        if image_evidence is None:
            raise AdapterError("compose-up requires a successful image inspect record")
        if release_sha is None:
            raise AdapterError("compose-up requires the approved release SHA")
        if require_absolute(approved_image or Path("/nonexistent"), name="approved image") != APPROVED_IMAGE_PATH:
            raise AdapterError("production approved image must use the fixed evidence path")
        if require_absolute(image_evidence or Path("/nonexistent"), name="image inspect evidence") != IMAGE_INSPECT_EVIDENCE_PATH:
            raise AdapterError("production image inspect evidence must use the fixed evidence path")
    argv = docker_argv(
        operation,
        digest=digest,
        compose_file=compose_file,
        executable=executable,
        production=production,
        test_seam=test_seam,
    )
    if production and executable != DOCKER:
        raise AdapterError("custom Docker executable is allowed only in the explicit test seam")
    checked_compose = None
    if operation in _COMPOSE_OPERATIONS:
        checked_compose = _validate_compose_file(compose_file, production=production)
        if production and operation == "compose-up":
            _validate_rendered_compose(checked_compose, compose_sha256 or "")
            _read_approved_image(approved_image or Path("/nonexistent"), digest or "", release_sha=release_sha)
            _read_image_evidence(image_evidence or Path("/nonexistent"), digest=digest or "")
    env = None if test_seam else production_command_environment()
    command = run_argv(
        argv,
        cwd=checked_compose.parent if checked_compose is not None else None,
        secret_values=secret_values,
        env=env,
    )
    evidence: dict[str, Any] = {
        "schema": "docker-operation-evidence-v1",
        "operation": operation,
        "requested_digest": digest,
        "argv": command["argv"],
        "returncode": command["returncode"],
        # This marker is deliberately not command output.  It preserves only
        # the fact that a bounded output stream existed.
        "output": "<redacted>" if command["stdout"] or command["stderr"] else "",
    }
    if release_sha is not None:
        evidence["release_sha"] = require_hex(release_sha, name="release SHA", length=40)
    if operation == "image-inspect":
        observed = _json_from_inspect(command["stdout"])
        evidence["image_digest"] = digest
        evidence["observed"] = _filtered_identity(observed, digest or "")
    elif checked_compose is not None and compose_sha256 is not None:
        evidence["compose_sha256"] = _validate_rendered_compose(checked_compose, compose_sha256)
    _write_evidence(output, evidence)
    return evidence


def _mode(args: argparse.Namespace) -> str:
    fixture = args.fixture_input is not None
    production = bool(args.production or args.mode == "production")
    seam = bool(args.test_seam or args.mode == "test-seam")
    if args.mode == "fixture" and (production or seam):
        raise AdapterError("fixture, production, and test-seam modes are mutually exclusive")
    if production and seam:
        raise AdapterError("production and test-seam modes are mutually exclusive")
    if production and (fixture or args.docker_executable is not None):
        raise AdapterError("production mode rejects fixture inputs and custom executables; custom executables require the explicit test seam")
    if seam and fixture:
        raise AdapterError("test seam rejects fixture inputs")
    if seam and args.docker_executable is None:
        raise AdapterError("test seam requires --docker-executable")
    if not production and not seam and not fixture:
        raise AdapterError("select --production explicitly or provide the existing fixture input")
    if args.mode == "fixture" and not fixture:
        raise AdapterError("fixture mode requires --fixture-input")
    return "production" if production else "test-seam" if seam else "fixture"


def _fixture(args: argparse.Namespace) -> int:
    if args.operation != "image-inspect" or args.fixture_input is None:
        raise AdapterError("fixture mode supports only image-inspect with --fixture-input")
    if args.digest is None:
        raise AdapterError("fixture image-inspect requires --digest")
    require_image_digest(args.digest)
    value = read_json(require_absolute(args.fixture_input, name="fixture input"), name="fixture input")
    if not isinstance(value, dict) or value.get("image_ref") != args.digest:
        raise AdapterError("digest mismatch")
    _write_evidence(args.output, value)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation")
    parser.add_argument("--mode", choices=("fixture", "production", "test-seam"))
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--test-seam", action="store_true")
    parser.add_argument("--fixture-input", type=Path)
    parser.add_argument("--digest")
    parser.add_argument("--compose-file", type=Path)
    parser.add_argument("--compose-sha256", "--rendered-compose-sha256", dest="compose_sha256")
    parser.add_argument("--approved-image", type=Path)
    parser.add_argument("--image-evidence", type=Path)
    parser.add_argument("--release-sha")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docker-executable", "--docker-path", "--fake-executable", dest="docker_executable", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        mode = _mode(args)
        if mode == "fixture":
            return _fixture(args)
        executable = DOCKER if mode == "production" else args.docker_executable
        run_adapter(
            args.operation,
            digest=args.digest,
            compose_file=args.compose_file,
            output=args.output,
            executable=executable or DOCKER,
            test_seam=mode == "test-seam",
            compose_sha256=args.compose_sha256,
            approved_image=args.approved_image,
            image_evidence=args.image_evidence,
            release_sha=args.release_sha,
        )
        return 0
    except (AdapterError, OSError, UnicodeDecodeError, subprocess.TimeoutExpired) as exc:  # type: ignore[name-defined]
        print(redact_text(str(exc), secret_values=environment_secret_values()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
