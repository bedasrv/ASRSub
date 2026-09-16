#!/usr/bin/env python3
"""Fail-closed Docker adapter for the immutable ASRSub deployment contract."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from production_adapter_common import (
    AdapterError,
    IMAGE_DIGEST_RE,
    atomic_write_json,
    default_owner,
    environment_secret_values,
    ensure_parent_directory,
    redact_text,
    require_absolute,
    require_image_digest,
    run_argv,
)


DOCKER = Path("/usr/bin/docker")
_OPERATION_ALIASES = {"digest": "image-inspect", "preflight": "image-inspect", "config": "compose-config"}
_OPERATIONS = {"image-inspect", "compose-config", "pull", "up"}


def canonical_operation(value: str) -> str:
    operation = _OPERATION_ALIASES.get(value, value)
    if operation not in _OPERATIONS:
        allowed = ", ".join(sorted(_OPERATIONS))
        raise AdapterError(f"operation is not allowlisted: {value}; allowed: {allowed}")
    return operation


def docker_argv(operation: str, *, digest: str | None = None, compose_file: Path | None = None, executable: Path = DOCKER) -> list[str]:
    operation = canonical_operation(operation)
    command = [os.fspath(executable), "--context", "default"]
    if operation == "image-inspect":
        if digest is None:
            raise AdapterError("image-inspect requires --digest")
        require_image_digest(digest)
        return command + ["image", "inspect", "--format", "{{json .}}", digest]
    if compose_file is None:
        raise AdapterError(f"{operation} requires --compose-file")
    compose_file = require_absolute(compose_file, name="compose file")
    if not compose_file.is_file():
        raise AdapterError(f"compose file is missing: {compose_file}")
    compose = ["compose", "-f", os.fspath(compose_file)]
    if operation == "compose-config":
        return command + compose + ["config", "--quiet"]
    if operation == "pull":
        return command + compose + ["pull", "--quiet"]
    return command + compose + ["up", "-d", "--no-build", "--pull=never"]


def _json_from_inspect(stdout: str) -> Any:
    try:
        value = json.loads(stdout.strip())
    except json.JSONDecodeError as exc:
        raise AdapterError(f"Docker image inspect returned non-JSON output: {exc}") from exc
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


def _write_evidence(output: Path, value: dict[str, Any]) -> None:
    output = require_absolute(output, name="Docker evidence output")
    ensure_parent_directory(output, name="Docker evidence output")
    uid, gid = default_owner()
    atomic_write_json(output, value, mode=0o600, uid=uid, gid=gid, name="Docker evidence output")


def run_adapter(
    operation: str,
    *,
    digest: str | None,
    compose_file: Path | None,
    output: Path,
    executable: Path = DOCKER,
    test_seam: bool = False,
) -> dict[str, Any]:
    operation = canonical_operation(operation)
    if operation == "image-inspect":
        if digest is None:
            raise AdapterError("image-inspect requires --digest")
        require_image_digest(digest)
    elif digest is not None:
        require_image_digest(digest)
    argv = docker_argv(operation, digest=digest, compose_file=compose_file, executable=executable)
    if not test_seam and executable != DOCKER:
        raise AdapterError("custom Docker executable is allowed only in the explicit test seam")
    command = run_argv(
        argv,
        cwd=None if compose_file is None else compose_file.parent,
        secret_values=environment_secret_values(),
    )
    evidence: dict[str, Any] = {
        "schema": "docker-operation-evidence-v1",
        "operation": operation,
        "requested_digest": digest,
        "argv": command["argv"],
        "returncode": command["returncode"],
        "stdout": command["stdout"],
        "stderr": command["stderr"],
    }
    if operation == "image-inspect":
        observed = _json_from_inspect(command["stdout"])
        if not _digest_in_observed(observed, digest or ""):
            raise AdapterError("Docker image inspect did not prove the requested digest")
        evidence["image_digest"] = digest
        evidence["observed"] = observed
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
    try:
        value = json.loads(args.fixture_input.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(f"cannot read fixture input: {exc}") from exc
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
        if mode == "production" and executable != DOCKER:
            raise AdapterError("custom Docker executable is allowed only in the explicit test seam")
        run_adapter(
            args.operation,
            digest=args.digest,
            compose_file=args.compose_file,
            output=args.output,
            executable=executable or DOCKER,
            test_seam=mode == "test-seam",
        )
        return 0
    except AdapterError as exc:
        print(redact_text(str(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
