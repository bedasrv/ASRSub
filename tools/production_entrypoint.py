#!/usr/bin/env python3
"""Fixed-path production entrypoint shipped inside the approved bundle.

The systemd wrappers invoke this file from the installed runtime root.  The
runtime root contains the adapter modules and rendered Compose projection; no
checkout path is consulted by production.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from production_adapter_common import (
    AdapterError,
    canonical_json,
    ensure_no_symlink,
    environment_secret_values,
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
OPENSSL = Path("/usr/bin/openssl")
APPROVAL = DEPLOY_STATE_ROOT / "approval.json"
BUNDLE_MANIFEST = DEPLOY_STATE_ROOT / "bundle-manifest.json"
APPROVED_IMAGE = DEPLOY_STATE_ROOT / "approved-image.json"
INSTALL_RECEIPT = DEPLOY_STATE_ROOT / "evidence" / "runtime-bundle-install.json"
EVIDENCE_ROOT = DEPLOY_STATE_ROOT / "evidence"
IMAGE_EVIDENCE = EVIDENCE_ROOT / "image-inspect.json"
COMPOSE_EVIDENCE = EVIDENCE_ROOT / "compose-config.json"
PULL_EVIDENCE = EVIDENCE_ROOT / "image-pull.json"
UP_EVIDENCE = EVIDENCE_ROOT / "compose-up.json"
TRUST_ROOT = DEPLOY_STATE_ROOT / "trust"
APPROVAL_SIGNATURE = TRUST_ROOT / "approval.sig"
APPROVAL_PUBLIC_KEY = TRUST_ROOT / "approval-key.pub"
BUNDLE_SIGNATURE = TRUST_ROOT / "bundle-manifest.sig"
BUNDLE_PUBLIC_KEY = TRUST_ROOT / "bundle-signing-key.pub"

# The nine operational members are kept closed.  Adapter support and the
# rendered Compose file are additional signed runtime members, not checkout
# files.  Systemd descriptors are signed members installed outside RUNTIME_ROOT.
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
    ("OpenSSL executable", OPENSSL, False),
    ("rendered Compose", COMPOSE_FILE, False),
    ("authenticated approval", APPROVAL, False),
    ("bundle manifest", BUNDLE_MANIFEST, False),
    ("approved image", APPROVED_IMAGE, False),
    ("install receipt", INSTALL_RECEIPT, False),
    ("approval signature", APPROVAL_SIGNATURE, False),
    ("approval trust anchor", APPROVAL_PUBLIC_KEY, False),
    ("bundle signature", BUNDLE_SIGNATURE, False),
    ("bundle trust anchor", BUNDLE_PUBLIC_KEY, False),
)


def _blocked(label: str) -> AdapterError:
    return AdapterError(f"production preflight blocked: missing or unsafe {label}")


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


def _verify_detached(*, signature: Path, public_key: Path, data: Path, label: str) -> None:
    if signature != (BUNDLE_SIGNATURE if label == "bundle" else APPROVAL_SIGNATURE):
        raise _blocked(f"{label} signature path")
    if public_key != (BUNDLE_PUBLIC_KEY if label == "bundle" else APPROVAL_PUBLIC_KEY):
        raise _blocked(f"{label} trust-anchor path")
    _require_artifact(f"{label} signature", signature, False)
    _require_artifact(f"{label} trust anchor", public_key, False)
    try:
        run_argv(
            [OPENSSL, "dgst", "-sha256", "-verify", public_key, "-signature", signature, data],
            cwd=TRUST_ROOT,
            secret_values=environment_secret_values(),
            env=production_command_environment(),
        )
    except AdapterError as exc:
        raise _blocked(f"invalid {label} detached signature") from exc


def _approved_transaction(manifest_hash: str, compose_sha256: str) -> dict[str, str]:
    approval = read_json(APPROVAL, name="authenticated approval")
    image = read_json(APPROVED_IMAGE, name="approved image")
    if not isinstance(approval, dict) or approval.get("schema") != "approval-v1":
        raise _blocked("authenticated approval schema")
    if not isinstance(image, dict) or image.get("schema") != "approved-image-v1":
        raise _blocked("approved image schema")
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
    if receipt.get("schema") != "runtime-bundle-install-receipt-v1" or receipt.get("dry_run") is not False or receipt.get("evidence_eligible") is not True:
        raise _blocked("production install receipt")
    receipt_approval = receipt.get("approval")
    if not isinstance(receipt_approval, dict) or receipt_approval.get("verified") is not True:
        raise _blocked("authenticated install receipt approval")
    if receipt.get("target_root") != os.fspath(RUNTIME_ROOT) or receipt.get("systemd_root") != os.fspath(SYSTEMD_ROOT) or receipt.get("release_sha") != receipt_approval.get("release_sha"):
        raise _blocked("install receipt target binding")
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
        else:
            target = SYSTEMD_ROOT / item["target"]
        _require_artifact(f"installed {item['path']}", target, False)
        try:
            mode = stat.S_IMODE(os.lstat(target).st_mode)
        except OSError as exc:
            raise _blocked(f"installed {item['path']} metadata") from exc
        if mode != item["mode"]:
            raise _blocked(f"installed {item['path']} mode")
        if sha256_file(target, name=f"installed {item['path']}") != item["sha256"]:
            raise _blocked(f"installed {item['path']} hash")


def preflight() -> dict[str, Any]:
    for label, path, directory in _REQUIRED_ARTIFACTS:
        _require_artifact(label, path, directory)
    manifest, members, manifest_hash = _read_manifest()
    compose_sha256 = sha256_file(COMPOSE_FILE, name="rendered Compose")
    if manifest.get("compose_sha256") not in (None, compose_sha256):
        raise _blocked("bundle Compose hash")
    _verify_detached(signature=BUNDLE_SIGNATURE, public_key=BUNDLE_PUBLIC_KEY, data=BUNDLE_MANIFEST, label="bundle")
    _verify_detached(signature=APPROVAL_SIGNATURE, public_key=APPROVAL_PUBLIC_KEY, data=APPROVAL, label="approval")
    approved = _approved_transaction(manifest_hash, compose_sha256)
    receipt = read_json(INSTALL_RECEIPT, name="install receipt")
    if not isinstance(receipt, dict) or receipt.get("release_sha") != approved["release_sha"] or receipt.get("manifest_sha256") != manifest_hash:
        raise _blocked("install receipt identity")
    receipt_approval = receipt.get("approval")
    if not isinstance(receipt_approval, dict) or any(
        receipt_approval.get(key) != expected
        for key, expected in (
            ("release_sha", approved["release_sha"]),
            ("bundle_sha256", manifest_hash),
            ("image_digest", approved["image_digest"].rsplit(":", 1)[-1]),
            ("compose_sha256", approved["compose_sha256"]),
        )
    ):
        raise _blocked("install receipt approval binding")
    _validate_installed_members(members, receipt)
    return approved


def reconcile() -> int:
    approved = preflight()
    # The module is a signed member beside this entrypoint.  Importing it from
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
