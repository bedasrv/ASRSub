#!/usr/bin/env python3
"""Validate and atomically install an authenticated ASRSub runtime bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from production_adapter_common import (
    AdapterError,
    atomic_write_json,
    canonical_json,
    default_owner,
    ensure_directory,
    ensure_directory_metadata,
    ensure_existing_directory,
    ensure_no_symlink,
    ensure_parent_directory,
    environment_secret_values,
    filesystem_identity,
    production_command_environment,
    read_json,
    redact_text,
    require_absolute,
    require_hex,
    run_argv,
    sha256_file,
)


PRODUCTION_OPENSSL = Path("/usr/bin/openssl")
PRODUCTION_BUNDLE_ROOT = Path("/var/lib/asrsub/deploy-state/bundle")
PRODUCTION_BUNDLE_MANIFEST = Path("/var/lib/asrsub/deploy-state/bundle-manifest.json")
PRODUCTION_APPROVAL = Path("/var/lib/asrsub/deploy-state/approval.json")
PRODUCTION_TARGET_ROOT = Path("/usr/local/libexec/asrsub")
PRODUCTION_SYSTEMD_ROOT = Path("/etc/systemd/system")
PRODUCTION_INSTALL_RECEIPT = Path("/var/lib/asrsub/deploy-state/evidence/runtime-bundle-install.json")
PRODUCTION_TRUST_ROOT = Path("/var/lib/asrsub/deploy-state/trust")
DEFAULT_APPROVAL_SIGNATURE = PRODUCTION_TRUST_ROOT / "approval.sig"
DEFAULT_APPROVAL_PUBLIC_KEY = PRODUCTION_TRUST_ROOT / "approval-key.pub"
DEFAULT_BUNDLE_SIGNATURE = PRODUCTION_TRUST_ROOT / "bundle-manifest.sig"
DEFAULT_BUNDLE_PUBLIC_KEY = PRODUCTION_TRUST_ROOT / "bundle-signing-key.pub"

# Nine operational files remain the closed runtime inventory.  The signed
# bundle also carries the fixed production adapters and rendered Compose file;
# systemd descriptors are signed members installed outside the runtime root.
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
EXPECTED_BUNDLE_MEMBERS = EXPECTED_INSTALLED_RUNTIME_MEMBERS | EXPECTED_SYSTEMD_MEMBERS
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
_ALLOWED_MANIFEST_SCHEMAS = {"runtime-bundle-manifest-v1", "bundle-manifest-v1"}


def _fixture() -> int:
    required = (
        Path("systemd/asrsub-recovery.service"),
        Path("systemd/asrsub-runtime.service"),
        Path("systemd/docker.service.d/asrsub-recovery.conf"),
    )
    for path in required:
        if not path.is_file():
            raise AdapterError(f"missing fixture {path}")
    return 0


def _member_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise AdapterError("manifest member path is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise AdapterError(f"manifest member path is unsafe: {value}")
    return "/".join(pure.parts)


def _parse_mode_value(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        mode = value
    elif isinstance(value, str):
        try:
            mode = int(value, 8)
        except ValueError as exc:
            raise AdapterError("invalid manifest mode") from exc
    else:
        raise AdapterError("manifest member mode is required")
    if mode < 0 or mode & ~0o777:
        raise AdapterError("manifest mode is unsafe")
    return mode


def _load_manifest(
    bundle_root: Path,
    manifest_path: Path,
    release_sha: str,
    *,
    expected_signing_mode: str = "production",
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    manifest_path = require_absolute(manifest_path, name="bundle manifest")
    ensure_no_symlink(manifest_path, name="bundle manifest", allow_missing=False)
    manifest = read_json(manifest_path, name="bundle manifest")
    if not isinstance(manifest, dict) or manifest.get("schema") not in _ALLOWED_MANIFEST_SCHEMAS:
        raise AdapterError("bundle manifest has an unsupported schema")
    if manifest.get("signing_mode") != expected_signing_mode:
        raise AdapterError(f"bundle manifest signing mode must be {expected_signing_mode}")
    if manifest.get("release_sha") != release_sha:
        raise AdapterError("bundle manifest release SHA does not match the caller")
    members = manifest.get("members")
    if not isinstance(members, list) or not members:
        raise AdapterError("bundle manifest must contain a non-empty members allowlist")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in members:
        if not isinstance(item, dict):
            raise AdapterError("bundle manifest member is not an object")
        path = _member_path(item.get("path"))
        if path in seen:
            raise AdapterError(f"duplicate bundle member: {path}")
        seen.add(path)
        digest = item.get("sha256")
        if not isinstance(digest, str) or not digest.islower() or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise AdapterError(f"invalid expected hash for bundle member: {path}")
        install_root = item.get("install_root", "runtime")
        if install_root not in {"runtime", "systemd"}:
            raise AdapterError(f"invalid install root for bundle member: {path}")
        if install_root == "systemd":
            if not path.startswith("systemd/") or path == "systemd/":
                raise AdapterError(f"invalid systemd bundle member path: {path}")
            target = path.removeprefix("systemd/")
        else:
            target = path
        normalized.append({"path": path, "target": target, "install_root": install_root, "sha256": digest, "mode": _parse_mode_value(item.get("mode"))})
    manifest_hash = hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest, normalized, manifest_hash


def _relative(path: Path, root: Path) -> str | None:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def _expected_directories(members: list[dict[str, Any]]) -> set[str]:
    directories: set[str] = set()
    for item in members:
        pure = PurePosixPath(item["path"])
        for index in range(1, len(pure.parts)):
            directories.add("/".join(pure.parts[:index]))
    return directories


def _validate_bundle_tree(
    bundle_root: Path,
    manifest_path: Path,
    members: list[dict[str, Any]],
    *,
    exact_inventory: bool,
) -> None:
    allowed = {item["path"] for item in members}
    manifest_rel = _relative(manifest_path, bundle_root)
    if manifest_rel is not None:
        allowed.add(manifest_rel)
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for current, directories, files in os.walk(bundle_root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            try:
                st = os.lstat(path)
            except OSError as exc:
                raise AdapterError("cannot inspect bundle directory") from exc
            if stat.S_ISLNK(st.st_mode):
                raise AdapterError(f"bundle contains a symlink: {path}")
            if not stat.S_ISDIR(st.st_mode):
                raise AdapterError(f"bundle contains an unsupported directory entry: {path}")
            actual_dirs.add(path.relative_to(bundle_root).as_posix())
        for name in files:
            path = current_path / name
            try:
                st = os.lstat(path)
            except OSError as exc:
                raise AdapterError("cannot inspect bundle member") from exc
            if stat.S_ISLNK(st.st_mode):
                raise AdapterError(f"bundle contains a symlink: {path}")
            relative = path.relative_to(bundle_root).as_posix()
            if not stat.S_ISREG(st.st_mode):
                raise AdapterError(f"bundle contains an unsupported entry: {relative}")
            if relative not in allowed:
                raise AdapterError(f"bundle contains an unlisted file: {relative}")
            if relative != manifest_rel:
                actual_files.add(relative)
    if exact_inventory and actual_files != {item["path"] for item in members}:
        missing = sorted({item["path"] for item in members} - actual_files)
        extra = sorted(actual_files - {item["path"] for item in members})
        raise AdapterError(f"runtime bundle inventory mismatch: missing={missing}, extra={extra}")
    expected_dirs = _expected_directories(members)
    if exact_inventory and actual_dirs != expected_dirs:
        missing_dirs = sorted(expected_dirs - actual_dirs)
        extra_dirs = sorted(actual_dirs - expected_dirs)
        raise AdapterError(f"runtime bundle directory inventory mismatch: missing={missing_dirs}, extra={extra_dirs}")
    for item in members:
        source = bundle_root / Path(item["path"])
        ensure_no_symlink(source, name="bundle member", allow_missing=False)
        try:
            st = os.lstat(source)
        except OSError as exc:
            raise AdapterError("cannot inspect bundle member") from exc
        if not stat.S_ISREG(st.st_mode):
            raise AdapterError(f"bundle member is not a regular file: {item['path']}")
        if stat.S_IMODE(st.st_mode) != item["mode"]:
            raise AdapterError(f"bundle member mode mismatch: {item['path']}")
        if sha256_file(source, name=f"bundle member {item['path']}") != item["sha256"]:
            raise AdapterError(f"bundle member hash mismatch: {item['path']}")



def _validate_systemd_contract(
    root: Path,
    members: list[dict[str, Any]] | None = None,
    *,
    uid: int | None = None,
    gid: int | None = None,
) -> None:
    root = require_absolute(root, name="systemd root")
    if uid is not None and gid is not None:
        ensure_directory_metadata(root, mode=0o755, uid=uid, gid=gid, name="systemd root")
    else:
        ensure_existing_directory(root, name="systemd root")
    required = EXPECTED_SYSTEMD_MEMBERS if members is None else {
        item["path"] for item in members if item.get("install_root") == "systemd"
    }
    if required != EXPECTED_SYSTEMD_MEMBERS:
        raise AdapterError("signed systemd inventory is incomplete")
    for relative in sorted(required):
        target = root / relative.removeprefix("systemd/")
        ensure_no_symlink(target, name="systemd contract", allow_missing=False)
        if target.parent != root and uid is not None and gid is not None:
            ensure_directory_metadata(
                target.parent,
                mode=0o755,
                uid=uid,
                gid=gid,
                name=f"systemd directory for {relative}",
            )
        try:
            st = os.lstat(target)
        except OSError as exc:
            raise AdapterError(f"cannot inspect systemd contract: {relative}") from exc
        if not stat.S_ISREG(st.st_mode):
            raise AdapterError(f"systemd contract member is not regular: {relative}")
        if stat.S_IMODE(st.st_mode) != SYSTEMD_MEMBER_MODES[relative]:
            raise AdapterError(f"systemd contract mode mismatch: {relative}")
        if uid is not None and st.st_uid != uid:
            raise AdapterError(f"systemd contract uid mismatch: {relative}")
        if gid is not None and st.st_gid != gid:
            raise AdapterError(f"systemd contract gid mismatch: {relative}")


def _validate_installed_systemd(root: Path, members: list[dict[str, Any]], *, uid: int, gid: int) -> None:
    _validate_systemd_contract(root, members, uid=uid, gid=gid)
    by_path = {item["path"]: item for item in members if item.get("install_root") == "systemd"}
    for relative, item in by_path.items():
        target = root / item["target"]
        if sha256_file(target, name=f"installed systemd member {relative}") != item["sha256"]:
            raise AdapterError(f"installed systemd member hash mismatch: {relative}")


def _read_approval(path: Path, *, expected_signing_mode: str = "production") -> dict[str, Any]:
    value = read_json(require_absolute(path, name="authenticated approval"), name="authenticated approval")
    if not isinstance(value, dict) or value.get("schema") != "approval-v1":
        raise AdapterError("authenticated approval has an unsupported schema")
    if value.get("signing_mode") != expected_signing_mode:
        raise AdapterError(f"authenticated approval signing mode must be {expected_signing_mode}")
    return value


def _bare_digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64 or value.lower() != value or any(char not in "0123456789abcdef" for char in value):
        raise AdapterError("approval image digest is not a lowercase sha256 identity")
    return value


def _validate_approval(
    approval: dict[str, Any],
    *,
    release_sha: str,
    manifest_hash: str,
    image_digest: str,
    compose_sha256: str,
    compose_template_sha256: str,
    state_root: str,
    generation: int | None,
    production: bool,
) -> None:
    expected_signing_mode = "production" if production else "test-seam"
    if approval.get("signing_mode") != expected_signing_mode:
        raise AdapterError(f"authenticated approval signing mode must be {expected_signing_mode}")
    if approval.get("release_sha") != release_sha:
        raise AdapterError("authenticated approval release SHA does not match the caller")
    if approval.get("bundle_sha256") != manifest_hash:
        raise AdapterError("authenticated approval bundle hash does not match the manifest")
    if _bare_digest(approval.get("image_digest")) != image_digest:
        raise AdapterError("authenticated approval image digest does not match the caller")
    if approval.get("compose_sha256") != compose_sha256:
        raise AdapterError("authenticated approval Compose hash does not match the caller")
    if approval.get("compose_template_sha256") != compose_template_sha256:
        raise AdapterError("authenticated approval template hash does not match the caller")
    approved_generation = approval.get("generation")
    if not isinstance(approved_generation, int) or isinstance(approved_generation, bool) or approved_generation <= 0:
        raise AdapterError("authenticated approval generation is invalid")
    if generation is not None and approved_generation != generation:
        raise AdapterError("authenticated approval generation does not match the caller")
    if approval.get("approved_docker_socket") != "default":
        raise AdapterError("authenticated approval Docker context is not default")
    if approval.get("approved_state_root") != state_root:
        raise AdapterError("authenticated approval state root does not match the fixed root")
    if production and state_root != "/var/lib/asrsub/state":
        raise AdapterError("production state root is not fixed")
    if production and set(approval).difference(
        {
            "schema",
            "signing_mode",
            "generation",
            "release_sha",
            "bundle_sha256",
            "image_digest",
            "compose_sha256",
            "compose_template_sha256",
            "notifications_enabled",
            "approved_docker_socket",
            "approved_state_root",
            "created_epoch_ns",
        }
    ):
        raise AdapterError("authenticated approval contains an unapproved binding")


def _require_fixed_path(path: Path, expected: Path, *, name: str) -> Path:
    path = require_absolute(path, name=name)
    if path != expected:
        raise AdapterError(f"{name} must use the fixed protected path")
    ensure_no_symlink(path, name=name, allow_missing=False)
    if not path.is_file():
        raise AdapterError(f"{name} is missing")
    return path


def _verify_detached(
    *,
    verifier: Path,
    signature: Path,
    public_key: Path,
    data: Path,
    label: str,
    test_seam: bool,
) -> None:
    ensure_no_symlink(signature, name=f"{label} signature", allow_missing=False)
    ensure_no_symlink(public_key, name=f"{label} trust anchor", allow_missing=False)
    if not signature.is_file() or not public_key.is_file():
        raise AdapterError(f"{label} detached signature material is missing")
    if test_seam:
        run_argv(
            [verifier, "--bundle-root", data.parent, "--manifest", data, "--approval", data, "--release-sha", "0" * 40],
            cwd=data.parent,
            secret_values=environment_secret_values(),
        )
        return
    if verifier != PRODUCTION_OPENSSL:
        raise AdapterError("production detached verification must use the fixed /usr/bin/openssl")
    run_argv(
        [verifier, "dgst", "-sha256", "-verify", public_key, "-signature", signature, data],
        cwd=data.parent,
        secret_values=environment_secret_values(),
        env=production_command_environment(),
    )


def _copy_verified(source: Path, destination: Path, *, mode: int, uid: int, gid: int) -> None:
    try:
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise AdapterError("cannot open bundle member") from exc
    destination_fd = -1
    try:
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        os.fchmod(destination_fd, mode)
        os.fchown(destination_fd, uid, gid)
        with os.fdopen(source_fd, "rb", closefd=True) as source_stream, os.fdopen(destination_fd, "wb", closefd=True) as destination_stream:
            source_fd = -1
            destination_fd = -1
            shutil.copyfileobj(source_stream, destination_stream, length=1024 * 1024)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
    except OSError as exc:
        raise AdapterError("cannot stage bundle member") from exc
    finally:
        for fd in (source_fd, destination_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise AdapterError("directory durability check failed") from exc


def _stage_bundle(
    bundle_root: Path,
    members: list[dict[str, Any]],
    stage: Path,
    *,
    uid: int,
    gid: int,
    systemd_stage: Path | None = None,
    systemd_uid: int | None = None,
    systemd_gid: int | None = None,
) -> None:
    for item in members:
        install_root = item.get("install_root", "runtime")
        if install_root == "systemd":
            if systemd_stage is None:
                raise AdapterError("systemd bundle member has no staging root")
            destination_root = systemd_stage
            destination_uid = uid if systemd_uid is None else systemd_uid
            destination_gid = gid if systemd_gid is None else systemd_gid
            relative = item["target"]
        else:
            destination_root = stage
            destination_uid = uid
            destination_gid = gid
            relative = item["target"]
        destination = destination_root / Path(relative)
        ensure_directory(destination.parent, mode=0o755, uid=destination_uid, gid=destination_gid, name="bundle staging directory")
        _copy_verified(bundle_root / Path(item["path"]), destination, mode=item["mode"], uid=destination_uid, gid=destination_gid)
        if sha256_file(destination, name="staged bundle member") != item["sha256"]:
            raise AdapterError(f"staged bundle member hash mismatch: {item['path']}")
        _fsync_directory(destination.parent)
    _fsync_directory(stage)
    if systemd_stage is not None:
        _fsync_directory(systemd_stage)


def _rollback_systemd_install(
    installed: list[tuple[Path, Path | None]],
    created_directories: list[Path] | None = None,
) -> None:
    for target, backup in reversed(installed):
        try:
            if os.path.lexists(target):
                target.unlink()
            if backup is not None and os.path.lexists(backup):
                os.replace(backup, target)
                _fsync_directory(target.parent)
        except OSError:
            # The caller reports the original failure.  A missing rollback is
            # still fail-closed because no receipt is emitted.
            pass
    for directory in reversed(created_directories or []):
        try:
            directory.rmdir()
        except OSError:
            pass


def _install_systemd_stage(
    stage: Path,
    root: Path,
    members: list[dict[str, Any]],
    *,
    uid: int,
    gid: int,
) -> tuple[list[tuple[Path, Path | None]], list[Path]]:
    root = ensure_existing_directory(root, name="systemd root")
    systemd_members = [item for item in members if item.get("install_root") == "systemd"]
    if {item["path"] for item in systemd_members} != EXPECTED_SYSTEMD_MEMBERS:
        raise AdapterError("signed systemd inventory is incomplete")
    installed: list[tuple[Path, Path | None]] = []
    created_directories: list[Path] = []
    try:
        for item in sorted(systemd_members, key=lambda value: value["target"]):
            source = stage / item["target"]
            target = root / item["target"]
            if not target.parent.exists():
                created_directories.append(target.parent)
            ensure_directory(target.parent, mode=0o755, uid=uid, gid=gid, name="systemd target directory")
            ensure_no_symlink(target, name="systemd target", allow_missing=True)
            backup: Path | None = None
            if os.path.lexists(target):
                backup = target.parent / f".{target.name}.asrsub-backup-{os.getpid()}"
                if os.path.lexists(backup):
                    raise AdapterError("systemd rollback path is occupied")
                os.replace(target, backup)
            installed.append((target, backup))
            os.replace(source, target)
            os.chmod(target, item["mode"])
            os.chown(target, uid, gid)
            _fsync_directory(target.parent)
        _validate_installed_systemd(root, members, uid=uid, gid=gid)
        _fsync_directory(root)
        return installed, created_directories
    except Exception:
        _rollback_systemd_install(installed, created_directories)
        raise


def _finalize_systemd_install(installed: list[tuple[Path, Path | None]]) -> None:
    for _, backup in installed:
        if backup is not None and os.path.lexists(backup):
            backup.unlink()
    parents = {target.parent for target, _ in installed}
    for parent in parents:
        _fsync_directory(parent)


def _target_files(target: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    for current, dirs, names in os.walk(target, followlinks=False):
        current_path = Path(current)
        for name in dirs:
            path = current_path / name
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise AdapterError("installed runtime contains an unsafe directory")
            directories.add(path.relative_to(target).as_posix())
        for name in names:
            path = current_path / name
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise AdapterError("installed runtime contains an unsafe member")
            files.add(path.relative_to(target).as_posix())
    return files, directories


def _validate_target(target: Path, members: list[dict[str, Any]], *, target_mode: int, uid: int, gid: int) -> None:
    ensure_existing_directory(target, name="bundle target")
    st = os.lstat(target)
    if stat.S_IMODE(st.st_mode) != target_mode or st.st_uid != uid or st.st_gid != gid:
        raise AdapterError("installed runtime root metadata does not match the contract")
    runtime_members = [item for item in members if item.get("install_root", "runtime") == "runtime"]
    files, directories = _target_files(target)
    expected_files = {item["target"] for item in runtime_members}
    expected_dirs = _expected_directories([{"path": item["target"]} for item in runtime_members])
    if files != expected_files or directories != expected_dirs:
        raise AdapterError("installed runtime inventory does not match the closed manifest")
    by_path = {item["target"]: item for item in runtime_members}
    for relative, item in by_path.items():
        path = target / Path(relative)
        st = os.lstat(path)
        if stat.S_IMODE(st.st_mode) != item["mode"] or st.st_uid != uid or st.st_gid != gid:
            raise AdapterError(f"installed runtime metadata mismatch: {relative}")
        if sha256_file(path, name=f"installed runtime member {relative}") != item["sha256"]:
            raise AdapterError(f"installed runtime hash mismatch: {relative}")


def _commit_stage(
    stage: Path,
    target: Path,
    *,
    target_mode: int,
    uid: int,
    gid: int,
    members: list[dict[str, Any]],
) -> tuple[bool, Path | None]:
    parent = ensure_parent_directory(target, name="bundle target")
    ensure_no_symlink(target, name="bundle target", allow_missing=True)
    target_exists = os.path.lexists(target)
    backup: Path | None = None
    if target_exists:
        ensure_existing_directory(target, name="bundle target")
        target_stat = os.lstat(target)
        if stat.S_IMODE(target_stat.st_mode) != target_mode or target_stat.st_uid != uid or target_stat.st_gid != gid:
            raise AdapterError("bundle target metadata does not match the contract")
        _target_files(target)  # reject symlinks and special entries before moving it
        backup = parent / f".{target.name}.backup-{os.getpid()}"
        if os.path.lexists(backup):
            raise AdapterError("bundle rollback path is occupied")
    try:
        if target_exists and backup is not None:
            os.replace(target, backup)
            _fsync_directory(parent)
        os.replace(stage, target)
        os.chmod(target, target_mode)
        os.chown(target, uid, gid)
        _fsync_directory(target)
        _fsync_directory(parent)
        _validate_target(target, members, target_mode=target_mode, uid=uid, gid=gid)
        return target_exists, backup
    except Exception:
        try:
            if os.path.lexists(target):
                if backup is not None and backup != target:
                    failed = parent / f".{target.name}.failed-{os.getpid()}-{stage.name}"
                    if not os.path.lexists(failed):
                        os.replace(target, failed)
                        shutil.rmtree(failed, ignore_errors=True)
                elif backup is None:
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink(missing_ok=True)
            if backup is not None and os.path.lexists(backup) and not os.path.lexists(target):
                os.replace(backup, target)
                _fsync_directory(parent)
        except OSError:
            pass
        raise


def _rollback_runtime(target: Path, state: tuple[bool, Path | None] | None) -> None:
    if state is None:
        return
    target_existed, backup = state
    parent = target.parent
    try:
        if not target_existed:
            if os.path.lexists(target):
                shutil.rmtree(target, ignore_errors=True)
            _fsync_directory(parent)
            return
        if os.path.lexists(target):
            failed = parent / f".{target.name}.failed-{os.getpid()}"
            if os.path.lexists(failed):
                shutil.rmtree(failed, ignore_errors=True)
            os.replace(target, failed)
            shutil.rmtree(failed, ignore_errors=True)
        if backup is not None and os.path.lexists(backup):
            os.replace(backup, target)
            _fsync_directory(parent)
    except OSError:
        pass


def _finalize_runtime(state: tuple[bool, Path | None] | None, parent: Path) -> None:
    if state is None:
        return
    _, backup = state
    if backup is not None and os.path.lexists(backup):
        shutil.rmtree(backup)
        _fsync_directory(parent)


def _production(args: argparse.Namespace, *, test_seam: bool) -> int:
    required = {
        "bundle root": args.bundle_root,
        "bundle manifest": args.manifest,
        "authenticated approval": args.approval,
        "release SHA": args.release_sha,
        "target root": args.target_root,
        "output": args.output,
    }
    for label, value in required.items():
        if value is None:
            raise AdapterError(f"{'test-seam' if test_seam else 'production'} mode requires {label}")
    if args.dry_run and not test_seam:
        raise AdapterError("dry-run is non-production and cannot be used for production evidence")
    if not test_seam and args.verify_command is not None and require_absolute(args.verify_command, name="verification command") != PRODUCTION_OPENSSL:
        raise AdapterError("production verification command must use the fixed /usr/bin/openssl")
    release_sha = require_hex(args.release_sha, name="release SHA", length=40)
    bundle_root_arg = require_absolute(args.bundle_root, name="bundle root")
    target = require_absolute(args.target_root, name="bundle target")
    manifest_path = require_absolute(args.manifest, name="bundle manifest")
    output_path = require_absolute(args.output, name="install receipt output")
    systemd_root = require_absolute(args.systemd_root, name="systemd root")
    if not test_seam:
        if bundle_root_arg != PRODUCTION_BUNDLE_ROOT:
            raise AdapterError("production bundle root must use the fixed approved path")
        if manifest_path != PRODUCTION_BUNDLE_MANIFEST:
            raise AdapterError("production bundle manifest must use the fixed approved path")
        if require_absolute(args.approval, name="authenticated approval") != PRODUCTION_APPROVAL:
            raise AdapterError("production approval must use the fixed approved path")
        if target != PRODUCTION_TARGET_ROOT:
            raise AdapterError("production bundle target must use the fixed runtime path")
        if output_path != PRODUCTION_INSTALL_RECEIPT:
            raise AdapterError("production install receipt must use the fixed evidence path")
        if systemd_root != PRODUCTION_SYSTEMD_ROOT:
            raise AdapterError("production systemd root must use the fixed approved path")
    bundle_root = ensure_existing_directory(bundle_root_arg, name="bundle root")
    if target == bundle_root:
        raise AdapterError("bundle target must differ from bundle root")
    uid, gid = (default_owner() if test_seam else (1000, 1000))
    systemd_uid, systemd_gid = (default_owner() if test_seam else (0, 0))
    target_mode = args.target_mode
    if target_mode != 0o755 or target_mode & ~0o777:
        raise AdapterError("bundle target mode must be exactly 0755")
    manifest, members, manifest_hash = _load_manifest(
        bundle_root,
        manifest_path,
        release_sha,
        expected_signing_mode="test-seam" if test_seam else "production",
    )
    runtime_members = [item for item in members if item.get("install_root", "runtime") == "runtime"]
    systemd_members = [item for item in members if item.get("install_root") == "systemd"]
    if systemd_members:
        ensure_directory_metadata(
            systemd_root,
            mode=0o755,
            uid=systemd_uid,
            gid=systemd_gid,
            name="systemd root",
        )
    if not test_seam:
        if {item["target"] for item in runtime_members} != EXPECTED_INSTALLED_RUNTIME_MEMBERS:
            raise AdapterError("runtime bundle manifest does not match the closed inventory")
        if {item["path"] for item in systemd_members} != EXPECTED_SYSTEMD_MEMBERS:
            raise AdapterError("systemd bundle manifest does not match the signed inventory")
        for item in runtime_members:
            if item["mode"] != RUNTIME_MEMBER_MODES[item["target"]]:
                raise AdapterError(f"runtime bundle executable mode mismatch: {item['target']}")
        for item in systemd_members:
            if item["mode"] != SYSTEMD_MEMBER_MODES[item["path"]]:
                raise AdapterError(f"systemd bundle mode mismatch: {item['path']}")
    _validate_bundle_tree(bundle_root, manifest_path, members, exact_inventory=not test_seam)

    if not test_seam:
        verifier = PRODUCTION_OPENSSL
        image_digest = _bare_digest(args.image_digest)
        compose_sha256 = require_hex(args.compose_sha256, name="rendered Compose SHA256", length=64)
        compose_template_sha256 = require_hex(args.compose_template_sha256, name="Compose template SHA256", length=64)
        state_root = "/var/lib/asrsub/state"
        generation = args.generation
        if generation is not None and generation <= 0:
            raise AdapterError("approval generation must be positive")
        approval = _read_approval(args.approval, expected_signing_mode="production")
        _validate_approval(
            approval,
            release_sha=release_sha,
            manifest_hash=manifest_hash,
            image_digest=image_digest,
            compose_sha256=compose_sha256,
            compose_template_sha256=compose_template_sha256,
            state_root=state_root,
            generation=generation,
            production=True,
        )
        approval_signature = _require_fixed_path(args.approval_signature or DEFAULT_APPROVAL_SIGNATURE, DEFAULT_APPROVAL_SIGNATURE, name="approval signature")
        approval_key = _require_fixed_path(args.approval_public_key or DEFAULT_APPROVAL_PUBLIC_KEY, DEFAULT_APPROVAL_PUBLIC_KEY, name="approval trust anchor")
        bundle_signature = _require_fixed_path(args.bundle_signature or DEFAULT_BUNDLE_SIGNATURE, DEFAULT_BUNDLE_SIGNATURE, name="bundle signature")
        bundle_key = _require_fixed_path(args.bundle_public_key or DEFAULT_BUNDLE_PUBLIC_KEY, DEFAULT_BUNDLE_PUBLIC_KEY, name="bundle trust anchor")
        _verify_detached(verifier=verifier, signature=bundle_signature, public_key=bundle_key, data=manifest_path, label="bundle", test_seam=False)
        _verify_detached(verifier=verifier, signature=approval_signature, public_key=approval_key, data=args.approval, label="approval", test_seam=False)
        approval_receipt = {
            "verified": True,
            "schema": approval["schema"],
            "generation": approval["generation"],
            "release_sha": release_sha,
            "bundle_sha256": manifest_hash,
            "image_digest": image_digest,
            "compose_sha256": compose_sha256,
            "compose_template_sha256": compose_template_sha256,
            "approved_docker_socket": "default",
            "approved_state_root": state_root,
            "signature_algorithm": "openssl-dgst-sha256",
        }
    else:
        verifier = args.verify_command
        if verifier is None:
            raise AdapterError("test-seam mode requires --verify-command")
        verifier = require_absolute(verifier, name="verification command")
        ensure_no_symlink(verifier, name="verification command", allow_missing=False)
        if not verifier.is_file() or not os.access(verifier, os.X_OK):
            raise AdapterError("verification command must be an executable regular file")
        # Fixture/test-seam approval remains intentionally non-production.
        _read_approval(args.approval, expected_signing_mode="test-seam")
        verification = run_argv(
            [verifier, "--bundle-root", bundle_root, "--manifest", manifest_path, "--approval", args.approval, "--release-sha", release_sha],
            cwd=bundle_root,
            secret_values=environment_secret_values(),
        )
        approval_receipt = {"verified": False, "test_seam": True, "verification_returncode": verification["returncode"]}

    target_identity = None
    if not args.dry_run:
        parent = ensure_parent_directory(target, name="bundle target")
        stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=parent))
        systemd_stage: Path | None = None
        runtime_state: tuple[bool, Path | None] | None = None
        systemd_installed: list[tuple[Path, Path | None]] = []
        systemd_created_directories: list[Path] = []
        try:
            os.chmod(stage, target_mode)
            os.chown(stage, uid, gid)
            _stage_bundle(
                bundle_root,
                runtime_members,
                stage,
                uid=uid,
                gid=gid,
                systemd_stage=None,
                systemd_uid=systemd_uid,
                systemd_gid=systemd_gid,
            )
            if systemd_members:
                systemd_stage = Path(tempfile.mkdtemp(prefix=".asrsub-systemd-stage-", dir=systemd_root))
                os.chmod(systemd_stage, 0o755)
                os.chown(systemd_stage, systemd_uid, systemd_gid)
                _stage_bundle(
                    bundle_root,
                    systemd_members,
                    stage,
                    uid=uid,
                    gid=gid,
                    systemd_stage=systemd_stage,
                    systemd_uid=systemd_uid,
                    systemd_gid=systemd_gid,
                )
            runtime_state = _commit_stage(
                stage,
                target,
                target_mode=target_mode,
                uid=uid,
                gid=gid,
                members=runtime_members,
            )
            if systemd_stage is not None:
                systemd_installed, systemd_created_directories = _install_systemd_stage(
                    systemd_stage,
                    systemd_root,
                    members,
                    uid=systemd_uid,
                    gid=systemd_gid,
                )
            _validate_target(target, runtime_members, target_mode=target_mode, uid=uid, gid=gid)
            if systemd_members:
                _validate_installed_systemd(systemd_root, members, uid=systemd_uid, gid=systemd_gid)
            target_identity = filesystem_identity(target, name="bundle target")
            _finalize_systemd_install(systemd_installed)
            systemd_installed = []
            systemd_created_directories = []
            _finalize_runtime(runtime_state, parent)
            runtime_state = None
        except Exception:
            _rollback_systemd_install(systemd_installed, systemd_created_directories)
            _rollback_runtime(target, runtime_state)
            shutil.rmtree(stage, ignore_errors=True)
            if systemd_stage is not None:
                shutil.rmtree(systemd_stage, ignore_errors=True)
            raise
        shutil.rmtree(stage, ignore_errors=True)
        if systemd_stage is not None:
            shutil.rmtree(systemd_stage, ignore_errors=True)

    receipt = {
        "schema": "runtime-bundle-install-dry-run-receipt-v1" if args.dry_run else "runtime-bundle-install-receipt-v1",
        "record_authority": "non-authoritative-install-record-v1",
        "release_sha": release_sha,
        "manifest_sha256": manifest_hash,
        "members": members,
        "approval": approval_receipt,
        "target_root": os.fspath(target),
        "systemd_root": os.fspath(systemd_root),
        "target_identity": target_identity,
        "dry_run": bool(args.dry_run),
        "evidence_eligible": False if args.dry_run or test_seam else True,
    }
    ensure_parent_directory(output_path, name="install receipt output")
    atomic_write_json(output_path, receipt, mode=0o600, uid=uid, gid=gid, name="install receipt")
    return 0


def _mode(args: argparse.Namespace) -> str:
    fixture = bool(args.check_fixture)
    production = bool(args.production or args.mode == "production")
    seam = bool(args.test_seam or args.mode == "test-seam")
    if args.mode == "fixture" and (production or seam):
        raise AdapterError("fixture, production, and test-seam modes are mutually exclusive")
    if production and (fixture or seam):
        raise AdapterError("production, fixture, and test-seam modes are mutually exclusive")
    if not production and not fixture and not seam:
        raise AdapterError("select --production, --test-seam, or --check-fixture")
    return "production" if production else "test-seam" if seam else "fixture"


def _octal(value: str) -> int:
    try:
        return int(value, 8)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("mode must be an octal value") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "production", "test-seam"))
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--test-seam", action="store_true")
    parser.add_argument("--check-fixture", action="store_true")
    parser.add_argument("--bundle-root", "--bundle-dir", dest="bundle_root", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--approval", "--authenticated-handoff", dest="approval", type=Path)
    parser.add_argument("--verify-command", "--verifier", dest="verify_command", type=Path)
    parser.add_argument("--approval-signature", type=Path)
    parser.add_argument("--approval-public-key", type=Path)
    parser.add_argument("--bundle-signature", type=Path)
    parser.add_argument("--bundle-public-key", type=Path)
    parser.add_argument("--release-sha")
    parser.add_argument("--image-digest")
    parser.add_argument("--compose-sha256", "--rendered-compose-sha256", dest="compose_sha256")
    parser.add_argument("--compose-template-sha256", dest="compose_template_sha256")
    parser.add_argument("--generation", type=int)
    parser.add_argument("--target-root", "--test-root", dest="target_root", type=Path)
    parser.add_argument("--systemd-root", type=Path, default=PRODUCTION_SYSTEMD_ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--target-mode", type=_octal, default=0o755)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        mode = _mode(args)
        if mode == "fixture":
            return _fixture()
        return _production(args, test_seam=mode == "test-seam")
    except (AdapterError, OSError, UnicodeDecodeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(redact_text(str(exc), secret_values=environment_secret_values()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
