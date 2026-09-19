#!/usr/bin/env python3
"""Fixed-path production entrypoint shipped inside the approved bundle.

The systemd wrappers invoke this file from the installed runtime root.  The
runtime root contains the adapter modules and rendered Compose projection; no
checkout path is consulted by production.  Release and member hashes are
validated directly from the local manifest and receipt."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
sys.dont_write_bytecode = True
import time
from pathlib import Path, PurePosixPath
from typing import Any

from production_adapter_common import (
    AdapterError,
    canonical_json,
    ensure_directory_metadata,
    ensure_no_symlink,
    ensure_regular_file,
    environment_secret_values,
    filesystem_identity,
    read_json,
    redact_text,
    production_command_environment,
    require_hex,
    require_image_digest,
    run_argv,
    sha256_file,
)


RUNTIME_ROOT = Path("/usr/local/libexec/asrsub")
COMPOSE_FILE = RUNTIME_ROOT / "compose.yaml"
ENTRYPOINT = RUNTIME_ROOT / "production_entrypoint.py"
ADAPTER_COMMON = RUNTIME_ROOT / "production_adapter_common.py"
DOCKER_ADAPTER = RUNTIME_ROOT / "deploy_docker.py"
STATE_ROOT = Path("/var/lib/asrsub/state")
DEPLOY_STATE_ROOT = Path("/var/lib/asrsub/deploy-state")
SYSTEMD_ROOT = Path("/etc/systemd/system")
DOCKER = Path("/usr/bin/docker")
APPROVAL = DEPLOY_STATE_ROOT / "approval.json"
BUNDLE_MANIFEST = DEPLOY_STATE_ROOT / "bundle-manifest.json"
APPROVED_IMAGE = DEPLOY_STATE_ROOT / "approved-image.json"
INSTALL_RECEIPT = DEPLOY_STATE_ROOT / "evidence" / "runtime-bundle-install.json"
EVIDENCE_ROOT = DEPLOY_STATE_ROOT / "evidence"
IMAGE_EVIDENCE = EVIDENCE_ROOT / "image-inspect.json"
COMPOSE_EVIDENCE = EVIDENCE_ROOT / "compose-config.json"
PULL_EVIDENCE = EVIDENCE_ROOT / "image-pull.json"
UP_EVIDENCE = EVIDENCE_ROOT / "compose-up.json"
PRODUCTION_UID = 1000
PRODUCTION_GID = 1000
SYSTEMD_UID = 0
SYSTEMD_GID = 0
RUNTIME_ROOT_MODE = 0o755
STATE_ROOT_MODE = 0o700
DEPLOY_STATE_ROOT_MODE = 0o700
EVIDENCE_ROOT_MODE = 0o700
SYSTEMD_ROOT_MODE = 0o755
SYSTEMD_DIRECTORY_MODE = 0o755
INSTALL_RECEIPT_AUTHORITY = "non-authoritative-install-record-v1"
HEALTH_PROBE = RUNTIME_ROOT / "asrsub-health-probe"
HEALTH_EVIDENCE = EVIDENCE_ROOT / "health.json"
HEALTH_PROBE_TIMEOUT = 10.0
HEALTH_PROBE_ATTEMPTS = 6
HEALTH_PROBE_DELAY = 1.0

# The nine operational members are kept closed.  Adapter support and the
# rendered Compose file are additional validated runtime members, not checkout
# files.  Systemd descriptors are validated members installed outside
# RUNTIME_ROOT.
CLOSED_RUNTIME_MEMBERS = frozenset(
    {
        "asrsub",
        "asrsub-state",
        "asrsub-record-rollout",
        "asrsub-generate-media-runtime-manifest",
        "asrsub-recover",
        "asrsub-runtime",
        "asrsub-health-probe",
        "asrsub-provision-statefs",
        "media-runtime-dependencies.json",
    }
)
RUNTIME_SUPPORT_MEMBERS = frozenset(
    {
        "production_entrypoint.py",
        "production_adapter_common.py",
        "deploy_docker.py",
        "compose.yaml",
    }
)
EXPECTED_RUNTIME_MEMBERS = CLOSED_RUNTIME_MEMBERS
EXPECTED_RUNTIME_SUPPORT_MEMBERS = RUNTIME_SUPPORT_MEMBERS
EXPECTED_INSTALLED_RUNTIME_MEMBERS = EXPECTED_RUNTIME_MEMBERS | EXPECTED_RUNTIME_SUPPORT_MEMBERS
EXPECTED_SYSTEMD_MEMBERS = frozenset(
    {
        "systemd/asrsub-recovery.service",
        "systemd/asrsub-runtime.service",
        "systemd/docker.service.d/asrsub-recovery.conf",
    }
)
EXPECTED_MANIFEST_MEMBERS = EXPECTED_INSTALLED_RUNTIME_MEMBERS | EXPECTED_SYSTEMD_MEMBERS
RUNTIME_EXECUTABLE_MEMBERS = frozenset(
    {
        "asrsub",
        "asrsub-state",
        "asrsub-record-rollout",
        "asrsub-generate-media-runtime-manifest",
        "asrsub-recover",
        "asrsub-runtime",
        "asrsub-health-probe",
        "asrsub-provision-statefs",
        "production_entrypoint.py",
    }
)
RUNTIME_MEMBER_MODES = {
    **{name: 0o755 for name in RUNTIME_EXECUTABLE_MEMBERS},
    "media-runtime-dependencies.json": 0o644,
    "production_adapter_common.py": 0o644,
    "deploy_docker.py": 0o644,
    "compose.yaml": 0o644,
}
SYSTEMD_MEMBER_MODES = {name: 0o644 for name in EXPECTED_SYSTEMD_MEMBERS}


_REQUIRED_ARTIFACTS = (
    ("runtime entrypoint", ENTRYPOINT, False),
    ("runtime adapter common module", ADAPTER_COMMON, False),
    ("runtime Docker adapter", DOCKER_ADAPTER, False),
    ("runtime root", RUNTIME_ROOT, True),
    ("state root", STATE_ROOT, True),
    ("deployment state", DEPLOY_STATE_ROOT, True),
    ("deployment evidence root", EVIDENCE_ROOT, True),
    ("Docker executable", DOCKER, False),
    ("rendered Compose", COMPOSE_FILE, False),
    ("approval", APPROVAL, False),
    ("bundle manifest", BUNDLE_MANIFEST, False),
    ("approved image", APPROVED_IMAGE, False),
    ("install receipt", INSTALL_RECEIPT, False),
)


def _blocked(label: str) -> AdapterError:
    return AdapterError(f"production preflight blocked: missing or unsafe {label}")


def _validate_host_metadata() -> None:
    ensure_directory_metadata(
        RUNTIME_ROOT,
        mode=RUNTIME_ROOT_MODE,
        uid=PRODUCTION_UID,
        gid=PRODUCTION_GID,
        name="runtime root metadata",
    )
    ensure_directory_metadata(
        STATE_ROOT,
        mode=STATE_ROOT_MODE,
        uid=PRODUCTION_UID,
        gid=PRODUCTION_GID,
        name="state root metadata",
    )
    ensure_directory_metadata(
        DEPLOY_STATE_ROOT,
        mode=DEPLOY_STATE_ROOT_MODE,
        uid=PRODUCTION_UID,
        gid=PRODUCTION_GID,
        name="deployment state metadata",
    )
    ensure_directory_metadata(
        EVIDENCE_ROOT,
        mode=EVIDENCE_ROOT_MODE,
        uid=PRODUCTION_UID,
        gid=PRODUCTION_GID,
        name="deployment evidence metadata",
    )
    ensure_directory_metadata(
        SYSTEMD_ROOT,
        mode=SYSTEMD_ROOT_MODE,
        uid=SYSTEMD_UID,
        gid=SYSTEMD_GID,
        name="systemd root metadata",
    )
    ensure_regular_file(DOCKER, mode=0o755, uid=0, gid=0, name="Docker executable metadata")
    protected_files = {
        APPROVAL: 0o600,
        BUNDLE_MANIFEST: 0o600,
        APPROVED_IMAGE: 0o600,
        INSTALL_RECEIPT: 0o600,
    }
    for path, mode in protected_files.items():
        ensure_regular_file(path, mode=mode, uid=PRODUCTION_UID, gid=PRODUCTION_GID, name=f"{path.name} metadata")
    dropin = SYSTEMD_ROOT / "docker.service.d"
    ensure_directory_metadata(
        dropin,
        mode=SYSTEMD_DIRECTORY_MODE,
        uid=SYSTEMD_UID,
        gid=SYSTEMD_GID,
        name="systemd drop-in directory metadata",
    )


def _require_artifact(label: str, path: Path, directory: bool) -> None:
    try:
        ensure_no_symlink(path, name=label, allow_missing=False)
        if directory and not path.is_dir():
            raise _blocked(label)
        if not directory and not path.is_file():
            raise _blocked(label)
    except AdapterError:
        raise
    except OSError as exc:
        raise _blocked(label) from exc


def _relative_member(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise _blocked(f"{label} member path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise _blocked(f"{label} member path")
    return pure.as_posix()


def _mode(value: Any, *, label: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value, 8)
        except ValueError as exc:
            raise _blocked(f"{label} mode") from exc
    else:
        raise _blocked(f"{label} mode")
    if result < 0 or result & ~0o777:
        raise _blocked(f"{label} mode")
    return result


def _manifest_member(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise _blocked("bundle manifest member")
    path = _relative_member(item.get("path"), label="bundle manifest")
    digest = item.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64 or digest.lower() != digest or any(char not in "0123456789abcdef" for char in digest):
        raise _blocked(f"bundle manifest hash for {path}")
    install_root = item.get("install_root", "runtime")
    if install_root not in {"runtime", "systemd"}:
        raise _blocked(f"bundle manifest install root for {path}")
    if install_root == "systemd":
        if not path.startswith("systemd/"):
            raise _blocked(f"bundle manifest systemd path for {path}")
        target = path.removeprefix("systemd/")
        if not target:
            raise _blocked("bundle manifest systemd path")
    else:
        target = path
    return {"path": path, "target": target, "install_root": install_root, "sha256": digest, "mode": _mode(item.get("mode"), label=path)}


def _read_manifest() -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    manifest = read_json(BUNDLE_MANIFEST, name="bundle manifest")
    if not isinstance(manifest, dict) or manifest.get("schema") not in {"runtime-bundle-manifest-v1", "bundle-manifest-v1"}:
        raise _blocked("bundle manifest schema")
    if manifest.get("integrity_mode") != "unsigned":
        raise _blocked("bundle manifest integrity mode")
    members = manifest.get("members")
    if not isinstance(members, list) or not members:
        raise _blocked("bundle manifest members")
    normalized = [_manifest_member(item) for item in members]
    if len({item["path"] for item in normalized}) != len(normalized):
        raise _blocked("duplicate bundle manifest member")
    if {item["path"] for item in normalized} != EXPECTED_MANIFEST_MEMBERS:
        raise _blocked("closed bundle manifest inventory")
    runtime = [item for item in normalized if item["install_root"] == "runtime"]
    systemd = [item for item in normalized if item["install_root"] == "systemd"]
    if {item["target"] for item in runtime} != EXPECTED_INSTALLED_RUNTIME_MEMBERS:
        raise _blocked("closed runtime inventory")
    if {item["path"] for item in systemd} != EXPECTED_SYSTEMD_MEMBERS:
        raise _blocked("closed systemd inventory")
    for item in runtime:
        expected = RUNTIME_MEMBER_MODES[item["target"]]
        if item["mode"] != expected:
            raise _blocked(f"runtime executable mode for {item['target']}")
    for item in systemd:
        if item["mode"] != SYSTEMD_MEMBER_MODES[item["path"]]:
            raise _blocked(f"systemd member mode for {item['path']}")
    release_sha = manifest.get("release_sha")
    if not isinstance(release_sha, str):
        raise _blocked("bundle manifest release binding")
    require_hex(release_sha, name="bundle manifest release SHA", length=40)
    manifest_hash = hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest, normalized, manifest_hash


def _approved_transaction(manifest_hash: str, compose_sha256: str) -> dict[str, str]:
    approval = read_json(APPROVAL, name="approval")
    image = read_json(APPROVED_IMAGE, name="approved image")
    if not isinstance(approval, dict) or approval.get("schema") != "approval-v1":
        raise _blocked("approval schema")
    if approval.get("integrity_mode") != "unsigned":
        raise _blocked("approval integrity mode")
    if not isinstance(image, dict) or image.get("schema") != "approved-image-v1":
        raise _blocked("approved image schema")
    if image.get("fixture_only") is True:
        raise _blocked("fixture approved image")
    release_sha = approval.get("release_sha")
    if not isinstance(release_sha, str):
        raise _blocked("approval release binding")
    release_sha = require_hex(release_sha, name="approved release SHA", length=40)
    if image.get("release_sha") != release_sha:
        raise _blocked("approved image release binding")
    image_ref = image.get("image_ref")
    if not isinstance(image_ref, str):
        raise _blocked("approved image identity")
    image_ref = require_image_digest(image_ref)
    bare = image_ref.rsplit(":", 1)[-1]
    if image.get("image_digest") != bare:
        raise _blocked("approved image digest binding")
    if image.get("platform") != "linux/amd64":
        raise _blocked("approved image platform")
    if approval.get("image_digest") != bare:
        raise _blocked("approval image binding")
    if approval.get("bundle_sha256") != manifest_hash:
        raise _blocked("approval bundle binding")
    if approval.get("compose_sha256") != compose_sha256:
        raise _blocked("approval Compose binding")
    if approval.get("approved_docker_socket") != "default" or approval.get("approved_state_root") != os.fspath(STATE_ROOT):
        raise _blocked("approval host binding")
    return {"image_digest": image_ref, "release_sha": release_sha, "compose_sha256": compose_sha256}


def _validate_installed_members(normalized: list[dict[str, Any]], receipt: dict[str, Any]) -> None:
    if not isinstance(receipt, dict):
        raise _blocked("production install receipt")
    if (
        receipt.get("schema") != "runtime-bundle-install-receipt-v1"
        or receipt.get("dry_run") is not False
        or receipt.get("evidence_eligible") is not True
        or receipt.get("record_authority") != INSTALL_RECEIPT_AUTHORITY
    ):
        raise _blocked("production install receipt")
    if receipt.get("target_root") != os.fspath(RUNTIME_ROOT) or receipt.get("systemd_root") != os.fspath(SYSTEMD_ROOT):
        raise _blocked("install receipt target binding")
    receipt_approval = receipt.get("approval")
    if receipt_approval is not None and not isinstance(receipt_approval, dict):
        raise _blocked("install receipt record")
    receipt_members = receipt.get("members")
    if not isinstance(receipt_members, list):
        raise _blocked("install receipt members")
    expected = {
        item["path"]: (item["sha256"], item["mode"], item["install_root"])
        for item in normalized
    }
    observed_receipt: dict[str, tuple[str, int, str]] = {}
    for raw in receipt_members:
        if not isinstance(raw, dict):
            raise _blocked("install receipt member")
        path = _relative_member(raw.get("path"), label="install receipt")
        scope = raw.get("install_root", "runtime")
        if scope not in {"runtime", "systemd"}:
            raise _blocked("install receipt member root")
        digest = raw.get("sha256")
        if not isinstance(digest, str):
            raise _blocked("install receipt member hash")
        observed_receipt[path] = (digest, _mode(raw.get("mode"), label=path), scope)
    if len(observed_receipt) != len(receipt_members) or observed_receipt != expected:
        raise _blocked("install receipt manifest binding")

    try:
        current_identity = filesystem_identity(RUNTIME_ROOT, name="installed runtime root")
    except AdapterError as exc:
        raise _blocked("installed runtime root identity") from exc
    receipt_identity = receipt.get("target_identity")
    if not isinstance(receipt_identity, dict) or receipt_identity != current_identity:
        raise _blocked("install receipt target identity")

    runtime_expected = {item["target"] for item in normalized if item["install_root"] == "runtime"}
    runtime_actual: set[str] = set()
    runtime_directories: set[str] = set()
    for current, directories, files in os.walk(RUNTIME_ROOT, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise _blocked("installed runtime symlink")
            runtime_directories.add(path.relative_to(RUNTIME_ROOT).as_posix())
        for name in files:
            path = current_path / name
            if path.is_symlink():
                raise _blocked("installed runtime symlink")
            relative = path.relative_to(RUNTIME_ROOT).as_posix()
            runtime_actual.add(relative)
    if runtime_actual != runtime_expected or runtime_directories:
        raise _blocked("installed runtime inventory")
    for item in normalized:
        if item["install_root"] == "runtime":
            target = RUNTIME_ROOT / item["target"]
            owner = (PRODUCTION_UID, PRODUCTION_GID)
        else:
            target = SYSTEMD_ROOT / item["target"]
            owner = (SYSTEMD_UID, SYSTEMD_GID)
            ensure_directory_metadata(
                target.parent,
                mode=SYSTEMD_DIRECTORY_MODE,
                uid=SYSTEMD_UID,
                gid=SYSTEMD_GID,
                name=f"installed systemd directory for {item['path']}",
            )
        ensure_regular_file(
            target,
            mode=item["mode"],
            uid=owner[0],
            gid=owner[1],
            name=f"installed {item['path']}",
        )
        if sha256_file(target, name=f"installed {item['path']}") != item["sha256"]:
            raise _blocked(f"installed {item['path']} hash")


def preflight() -> dict[str, Any]:
    for label, path, directory in _REQUIRED_ARTIFACTS:
        _require_artifact(label, path, directory)
    _validate_host_metadata()
    manifest, members, manifest_hash = _read_manifest()
    compose_sha256 = sha256_file(COMPOSE_FILE, name="rendered Compose")
    if manifest.get("compose_sha256") not in (None, compose_sha256):
        raise _blocked("bundle Compose hash")
    approved = _approved_transaction(manifest_hash, compose_sha256)
    receipt = read_json(INSTALL_RECEIPT, name="install receipt")
    if (
        not isinstance(receipt, dict)
        or receipt.get("release_sha") != approved["release_sha"]
        or receipt.get("manifest_sha256") != manifest_hash
    ):
        raise _blocked("install receipt identity")
    _validate_installed_members(members, receipt)
    return approved


def _wait_for_ready(
    *,
    probe: Path = HEALTH_PROBE,
    output: Path = HEALTH_EVIDENCE,
    runner=run_argv,
    sleep=time.sleep,
    attempts: int = HEALTH_PROBE_ATTEMPTS,
    delay: float = HEALTH_PROBE_DELAY,
) -> dict[str, Any]:
    if attempts <= 0 or attempts > 12 or delay < 0:
        raise AdapterError("health readiness bounds are invalid")
    production_runner = runner is run_argv
    if production_runner:
        if probe != HEALTH_PROBE or output != HEALTH_EVIDENCE:
            raise _blocked("health probe path")
        ensure_regular_file(probe, mode=0o755, uid=PRODUCTION_UID, gid=PRODUCTION_GID, name="installed health probe")
        cwd = RUNTIME_ROOT
        env = production_command_environment()
    else:
        cwd = None
        env = None
    last_error: AdapterError | None = None
    for index in range(attempts):
        ensure_no_symlink(output, name="health evidence", allow_missing=True)
        if output.exists():
            try:
                output.unlink()
            except OSError as exc:
                raise _blocked("stale health evidence") from exc
        try:
            runner(
                [probe, "--output", output],
                cwd=cwd,
                timeout=HEALTH_PROBE_TIMEOUT,
                secret_values=environment_secret_values(),
                env=env,
            )
        except AdapterError as exc:
            last_error = exc
        try:
            value = read_json(output, name="health evidence")
        except AdapterError as exc:
            last_error = exc
            value = None
        if (
            isinstance(value, dict)
            and value.get("schema") == "health-evidence-v1"
            and value.get("endpoint") == "/ready"
            and value.get("status") == 200
            and value.get("returncode") == 0
            and value.get("ready") is True
        ):
            return {"schema": value["schema"], "endpoint": "/ready", "status": 200, "returncode": 0, "ready": True}
        if index + 1 < attempts:
            sleep(delay)
    detail = "readiness probe did not observe /ready"
    if last_error is not None:
        detail = f"{detail}: {redact_text(str(last_error))}"
    raise AdapterError(detail)


def reconcile() -> int:
    approved = preflight()
    # The module is a validated member beside this entrypoint.  Importing it from
    # the fixed runtime directory avoids any dependency on the source checkout.
    if str(RUNTIME_ROOT) not in sys.path:
        sys.path.insert(0, str(RUNTIME_ROOT))
    try:
        from deploy_docker import run_adapter  # type: ignore
    except (ImportError, OSError) as exc:
        raise _blocked("installed Docker adapter") from exc

    run_adapter(
        "image-pull",
        digest=approved["image_digest"],
        compose_file=None,
        output=PULL_EVIDENCE,
        approved_image=APPROVED_IMAGE,
        release_sha=approved["release_sha"],
    )
    run_adapter(
        "image-inspect",
        digest=approved["image_digest"],
        compose_file=None,
        output=IMAGE_EVIDENCE,
        approved_image=APPROVED_IMAGE,
        release_sha=approved["release_sha"],
    )
    run_adapter(
        "compose-config",
        digest=approved["image_digest"],
        compose_file=COMPOSE_FILE,
        output=COMPOSE_EVIDENCE,
        approved_image=APPROVED_IMAGE,
        compose_sha256=approved["compose_sha256"],
        release_sha=approved["release_sha"],
    )
    run_adapter(
        "compose-up",
        digest=approved["image_digest"],
        compose_file=COMPOSE_FILE,
        output=UP_EVIDENCE,
        compose_sha256=approved["compose_sha256"],
        approved_image=APPROVED_IMAGE,
        image_evidence=IMAGE_EVIDENCE,
        pull_evidence=PULL_EVIDENCE,
        release_sha=approved["release_sha"],
    )
    _wait_for_ready()
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
        print(redact_text(str(exc), secret_values=environment_secret_values()), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
