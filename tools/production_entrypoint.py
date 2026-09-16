#!/usr/bin/env python3
"""Fixed-path production entrypoint used by the systemd wrappers.

This module deliberately has no caller-controlled production paths.  Fixture
harnesses invoke the adapter modules directly with their explicit fixture
switches; these entrypoints only operate on the approved host layout.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from production_adapter_common import (
    AdapterError,
    environment_secret_values,
    ensure_no_symlink,
    read_json,
    redact_text,
    require_image_digest,
    require_hex,
)


REPOSITORY_ROOT = Path("/opt/mediastack/asrsub")
STATE_ROOT = Path("/var/lib/asrsub/state")
DEPLOY_STATE_ROOT = Path("/var/lib/asrsub/deploy-state")
RUNTIME_ROOT = Path("/usr/local/libexec/asrsub")
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
DOCKER = Path("/usr/bin/docker")
APPROVAL = DEPLOY_STATE_ROOT / "approval.json"
BUNDLE_MANIFEST = DEPLOY_STATE_ROOT / "bundle-manifest.json"
APPROVED_IMAGE = DEPLOY_STATE_ROOT / "approved-image.json"
IMAGE_EVIDENCE = DEPLOY_STATE_ROOT / "evidence" / "image-inspect.json"
COMPOSE_EVIDENCE = DEPLOY_STATE_ROOT / "evidence" / "compose-config.json"
PULL_EVIDENCE = DEPLOY_STATE_ROOT / "evidence" / "image-pull.json"
UP_EVIDENCE = DEPLOY_STATE_ROOT / "evidence" / "compose-up.json"


_REQUIRED_ARTIFACTS = (
    ("repository adapter", REPOSITORY_ROOT / "tools" / "deploy_docker.py"),
    ("state root", STATE_ROOT),
    ("deployment state", DEPLOY_STATE_ROOT),
    ("runtime bundle", RUNTIME_ROOT),
    ("Docker executable", DOCKER),
    ("rendered Compose", COMPOSE_FILE),
    ("authenticated approval", APPROVAL),
    ("bundle manifest", BUNDLE_MANIFEST),
    ("approved image", APPROVED_IMAGE),
    ("deployment evidence root", DEPLOY_STATE_ROOT / "evidence"),
)


def _blocked(label: str) -> AdapterError:
    return AdapterError(f"production preflight blocked: missing or unsafe {label}")


def _require_artifact(label: str, path: Path, *, directory: bool = False) -> None:
    try:
        ensure_no_symlink(path, name=label, allow_missing=False)
        if directory and not path.is_dir():
            raise _blocked(label)
        if not directory and not path.is_file():
            raise _blocked(label)
    except AdapterError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise _blocked(label) from exc


def _approved_transaction() -> dict[str, str]:
    try:
        image_value = read_json(APPROVED_IMAGE, name="approved image")
        approval = read_json(APPROVAL, name="authenticated approval")
    except AdapterError as exc:
        raise _blocked("authenticated approval") from exc
    if not isinstance(image_value, dict) or image_value.get("schema") != "approved-image-v1":
        raise _blocked("approved image identity")
    image_ref = image_value.get("image_ref")
    if not isinstance(image_ref, str):
        raise _blocked("approved image identity")
    if not isinstance(approval, dict) or approval.get("schema") != "approval-v1":
        raise _blocked("authenticated approval schema")
    release_raw = approval.get("release_sha")
    compose_raw = approval.get("compose_sha256")
    if not isinstance(release_raw, str) or not isinstance(compose_raw, str):
        raise _blocked("authenticated release binding")
    try:
        digest = require_image_digest(image_ref)
        release_sha = require_hex(release_raw, name="approved release SHA", length=40)
        compose_sha256 = require_hex(compose_raw, name="approved Compose SHA256", length=64)
    except AdapterError as exc:
        raise _blocked("authenticated release binding") from exc
    image_digest = image_value.get("image_digest")
    if image_digest != digest.rsplit(":", 1)[-1].removeprefix("sha256:"):
        raise _blocked("approved image identity")
    if not isinstance(approval, dict) or approval.get("schema") != "approval-v1" or approval.get("image_digest") != image_digest:
        raise _blocked("authenticated approval image binding")
    if approval.get("approved_docker_socket") != "default" or approval.get("approved_state_root") != os.fspath(STATE_ROOT):
        raise _blocked("authenticated host binding")
    return {"image_digest": digest, "release_sha": release_sha, "compose_sha256": compose_sha256}


def preflight() -> dict[str, Any]:
    for label, path in _REQUIRED_ARTIFACTS:
        _require_artifact(
            label,
            path,
            directory=path in {REPOSITORY_ROOT, STATE_ROOT, DEPLOY_STATE_ROOT, RUNTIME_ROOT, DEPLOY_STATE_ROOT / "evidence"},
        )
    transaction = _approved_transaction()
    if COMPOSE_FILE.parent != REPOSITORY_ROOT:
        raise _blocked("rendered Compose location")
    return transaction


def reconcile() -> int:
    approved = preflight()
    # Import only after the fixed-path preflight.  This keeps a failed recovery
    # gate from reaching Docker or any mutation-capable adapter operation.
    tools_root = REPOSITORY_ROOT / "tools"
    if str(tools_root) not in sys.path:
        sys.path.insert(0, str(tools_root))
    try:
        from deploy_docker import run_adapter  # type: ignore
    except (ImportError, OSError) as exc:
        raise _blocked("Docker adapter") from exc

    run_adapter(
        "image-inspect",
        digest=approved["image_digest"],
        compose_file=None,
        output=IMAGE_EVIDENCE,
        release_sha=approved["release_sha"],
    )
    run_adapter(
        "compose-config",
        digest=approved["image_digest"],
        compose_file=COMPOSE_FILE,
        output=COMPOSE_EVIDENCE,
        compose_sha256=approved["compose_sha256"],
        release_sha=approved["release_sha"],
    )
    run_adapter("image-pull", digest=approved["image_digest"], compose_file=None, output=PULL_EVIDENCE, release_sha=approved["release_sha"])
    run_adapter(
        "compose-up",
        digest=approved["image_digest"],
        compose_file=COMPOSE_FILE,
        output=UP_EVIDENCE,
        compose_sha256=approved["compose_sha256"],
        approved_image=APPROVED_IMAGE,
        image_evidence=IMAGE_EVIDENCE,
        release_sha=approved["release_sha"],
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) != 2 or args[0] not in {"recover", "runtime"}:
            raise AdapterError("production preflight blocked: unsupported entrypoint request")
        role, action = args
        if role == "recover" and action != "--preflight":
            raise AdapterError("production preflight blocked: recovery action is not allowlisted")
        if role == "runtime" and action != "--reconcile":
            raise AdapterError("production preflight blocked: runtime action is not allowlisted")
        if role == "recover":
            preflight()
            return 0
        return reconcile()
    except (AdapterError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        values = environment_secret_values()
        print(redact_text(str(exc), secret_values=values), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
