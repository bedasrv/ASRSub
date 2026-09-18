#!/usr/bin/env python3
"""Collect bounded rollout evidence without accepting caller self-attestation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from production_adapter_common import (
    AdapterError,
    atomic_write_json,
    canonical_json,
    default_owner,
    ensure_directory_metadata,
    ensure_existing_directory,
    ensure_no_symlink,
    ensure_parent_directory,
    ensure_regular_file,
    environment_secret_values,
    filesystem_identity,
    production_command_environment,
    read_json,
    redact_text,
    require_absolute,
    require_hex,
    require_image_digest,
    run_argv,
    sha256_file,
)
from deploy_docker import COMPOSE_FILE, DOCKER, PROJECT_DIRECTORY, docker_argv


PRODUCTION_STATE_ROOT = Path("/var/lib/asrsub/state")
PRODUCTION_RUNTIME_ROOT = Path("/usr/local/libexec/asrsub")
PRODUCTION_HEALTH_PROBE = PRODUCTION_RUNTIME_ROOT / "asrsub-health-probe"
PRODUCTION_SYSTEMD_ROOT = Path("/etc/systemd/system")
PRODUCTION_CGROUP_ROOT = Path("/sys/fs/cgroup/system.slice/asrsub-runtime.service/asrsub-children")
PRODUCTION_EVIDENCE_ROOT = Path("/var/lib/asrsub/deploy-state/evidence")
PRODUCTION_ROLLOUT_OUTPUT = PRODUCTION_EVIDENCE_ROOT / "rollout.json"
PRODUCTION_DOCKER_EVIDENCE = PRODUCTION_EVIDENCE_ROOT / "image-inspect.json"
PRODUCTION_HEALTH_EVIDENCE = PRODUCTION_EVIDENCE_ROOT / "health.json"
PRODUCTION_BUNDLE_RECEIPT = PRODUCTION_EVIDENCE_ROOT / "runtime-bundle-install.json"
PRODUCTION_STATEFS_RECEIPT = PRODUCTION_EVIDENCE_ROOT / "statefs-provision" / "statefs-provision-receipt.json"
PRODUCTION_BUNDLE_MANIFEST = Path("/var/lib/asrsub/deploy-state/bundle-manifest.json")
PRODUCTION_APPROVAL = Path("/var/lib/asrsub/deploy-state/approval.json")
ALLOWED_STATEFS = frozenset({"ext4", "xfs", "btrfs", "zfs"})
REQUIRED_CGROUP_CONTROLLERS = frozenset({"cpu", "memory", "pids"})
EXPECTED_RUNTIME_MEMBERS = frozenset(
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
EXPECTED_RUNTIME_SUPPORT_MEMBERS = frozenset(
    {
        "production_entrypoint.py",
        "production_adapter_common.py",
        "deploy_docker.py",
        "compose.yaml",
    }
)
EXPECTED_INSTALLED_RUNTIME_MEMBERS = EXPECTED_RUNTIME_MEMBERS | EXPECTED_RUNTIME_SUPPORT_MEMBERS
EXPECTED_SYSTEMD_MEMBERS = frozenset(
    {
        "systemd/asrsub-recovery.service",
        "systemd/asrsub-runtime.service",
        "systemd/docker.service.d/asrsub-recovery.conf",
    }
)
EXPECTED_STATE_FILES = frozenset(
    {
        "state.jsonl",
        "state.jsonl.lock",
        "discord-notifications/state.json.lock",
        "deployment-admission/admission.lock",
        "deployment-admission/admission.json",
    }
)
EXPECTED_STATE_DIRECTORIES = frozenset(
    {
        "discord-notifications",
        "discord-notifications/quarantine",
        "deployment-admission",
    }
)
EXPECTED_SYSTEMD_FILES = frozenset(
    {
        "asrsub-recovery.service",
        "asrsub-runtime.service",
        "docker.service.d/asrsub-recovery.conf",
    }
)
EXPECTED_SYSTEMD_DIRECTORIES = frozenset({"docker.service.d"})
EXPECTED_CGROUP_FILES = frozenset({"cgroup.controllers", "cgroup.procs", "cgroup.subtree_control"})
RUNTIME_EXECUTABLES = frozenset(
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
EXPECTED_RUNTIME_MODES = {
    **{name: 0o755 for name in RUNTIME_EXECUTABLES},
    **{name: 0o644 for name in EXPECTED_INSTALLED_RUNTIME_MEMBERS - RUNTIME_EXECUTABLES},
}


def _fixture(args: argparse.Namespace) -> int:
    if args.fixture is None or args.output is None:
        raise AdapterError("fixture mode requires --fixture and --output")
    value = read_json(require_absolute(args.fixture, name="rollout fixture"), name="rollout fixture")
    if not isinstance(value, dict):
        raise AdapterError("rollout fixture must be an object")
    receipt = {
        "schema": "rollout-receipt-v1",
        "kind": "fixture",
        "result": "success",
        "evidence_paths": [],
        "evidence_eligible": False,
    }
    output = require_absolute(args.output, name="rollout receipt output")
    ensure_parent_directory(output, name="rollout receipt output")
    uid, gid = default_owner()
    atomic_write_json(output, receipt, mode=0o600, uid=uid, gid=gid, name="rollout receipt")
    return 0


def _tree_hash(entries: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(canonical_json({"path": entry["path"], "mode": entry["mode"], "sha256": entry["sha256"]}))
        digest.update(b"\n")
    return digest.hexdigest()


def _collect_tree(
    root: Path,
    *,
    label: str,
    exact_files: set[str] | frozenset[str] | None = None,
    exact_dirs: set[str] | frozenset[str] | None = None,
    required_files: set[str] | frozenset[str] | None = None,
    required_dirs: set[str] | frozenset[str] | None = None,
    tolerate_unrelated_unsafe: bool = False,
    allow_unobservable_mount: bool = False,
) -> dict[str, Any]:
    root = ensure_existing_directory(require_absolute(root, name=label), name=label)
    required_file_set = set(required_files or ())
    required_dir_set = set(required_dirs or ())
    entries: list[dict[str, Any]] = []
    directory_entries: list[dict[str, Any]] = []
    directories: set[str] = set()
    for current, directory_names, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directory_names:
            path = current_path / name
            try:
                st = os.lstat(path)
            except OSError as exc:
                raise AdapterError(f"cannot inspect {label}") from exc
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                relative = path.relative_to(root).as_posix()
                if tolerate_unrelated_unsafe and relative not in required_dir_set:
                    continue
                raise AdapterError(f"{label} contains an unsafe directory")
            relative = path.relative_to(root).as_posix()
            directories.add(relative)
            directory_entries.append(
                {
                    "path": relative,
                    "mode": stat.S_IMODE(st.st_mode),
                    "uid": st.st_uid,
                    "gid": st.st_gid,
                    "identity": filesystem_identity(
                        path,
                        name=f"{label} directory",
                        allow_unobservable_mount=allow_unobservable_mount,
                    ),
                }
            )
        for name in files:
            path = current_path / name
            try:
                st = os.lstat(path)
            except OSError as exc:
                raise AdapterError(f"cannot inspect {label}") from exc
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                if tolerate_unrelated_unsafe and relative not in required_file_set:
                    continue
                raise AdapterError(f"{label} contains an unsafe file")
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "mode": stat.S_IMODE(st.st_mode),
                    "uid": st.st_uid,
                    "gid": st.st_gid,
                    "sha256": sha256_file(path, name=f"{label} member"),
                    "identity": filesystem_identity(
                        path,
                        name=f"{label} member",
                        allow_unobservable_mount=allow_unobservable_mount,
                    ),
                }
            )
    entries.sort(key=lambda entry: entry["path"])
    directory_entries.sort(key=lambda entry: entry["path"])
    observed_files = {entry["path"] for entry in entries}
    if exact_files is not None and observed_files != set(exact_files):
        missing = sorted(set(exact_files) - observed_files)
        extra = sorted(observed_files - set(exact_files))
        raise AdapterError(f"{label} inventory mismatch: missing={missing}, extra={extra}")
    if required_files is not None and not set(required_files).issubset(observed_files):
        missing = sorted(set(required_files) - observed_files)
        raise AdapterError(f"{label} required entries missing: {missing}")
    if exact_dirs is not None and directories != set(exact_dirs):
        missing = sorted(set(exact_dirs) - directories)
        extra = sorted(directories - set(exact_dirs))
        raise AdapterError(f"{label} directory inventory mismatch: missing={missing}, extra={extra}")
    if required_dirs is not None and not set(required_dirs).issubset(directories):
        missing = sorted(set(required_dirs) - directories)
        raise AdapterError(f"{label} required directories missing: {missing}")
    if not entries:
        raise AdapterError(f"{label} has no observable regular files")
    return {
        "root": os.fspath(root),
        "root_identity": filesystem_identity(
            root,
            name=label,
            allow_unobservable_mount=allow_unobservable_mount,
        ),
        "observed": True,
        "entries": entries,
        "directories": directory_entries,
        "tree_sha256": _tree_hash(entries),
    }


def _contains_digest(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected or value == expected.rsplit("@", 1)[-1]
    if isinstance(value, list):
        return any(_contains_digest(item, expected) for item in value)
    if isinstance(value, dict):
        return any(_contains_digest(item, expected) for item in value.values())
    return False


def _docker_evidence(path: Path, *, release_sha: str, image_digest: str, production: bool) -> dict[str, Any]:
    value = read_json(require_absolute(path, name="Docker evidence"), name="Docker evidence")
    if not isinstance(value, dict) or value.get("schema") != "docker-operation-evidence-v1":
        raise AdapterError("Docker evidence has an unsupported schema")
    if production:
        if value.get("operation") != "image-inspect" or value.get("returncode") != 0:
            raise AdapterError("Docker evidence is not a successful image inspect")
        if value.get("requested_digest") != image_digest or value.get("image_digest") != image_digest:
            raise AdapterError("Docker evidence digest does not match the caller")
        if value.get("release_sha") != release_sha:
            raise AdapterError("Docker evidence release SHA does not match the caller")
        expected_argv = docker_argv("image-inspect", digest=image_digest, production=True)
        if value.get("argv") != expected_argv:
            raise AdapterError("Docker evidence command is not the fixed image-inspect command")
        if any(key in value for key in ("stdout", "stderr")):
            raise AdapterError("Docker evidence contains an unfiltered output field")
    else:
        if value.get("requested_digest") not in (None, image_digest) or value.get("image_digest") not in (None, image_digest):
            raise AdapterError("Docker evidence digest does not match the caller")
        if value.get("release_sha") not in (None, release_sha):
            raise AdapterError("Docker evidence release SHA does not match the caller")
    observed = value.get("observed")
    if not isinstance(observed, dict) or not _contains_digest(observed, image_digest):
        raise AdapterError("Docker evidence does not prove the requested image digest")
    summary: dict[str, Any] = {
        "schema": value["schema"],
        "operation": "image-inspect",
        "requested_digest": image_digest,
        "image_digest": image_digest,
        "release_sha": release_sha,
        "argv": [redact_text(str(item), secret_values=environment_secret_values()) for item in value.get("argv", [])],
        "returncode": 0,
        "observed": {
            key: observed[key]
            for key in ("image_ref", "image_digest", "Id", "id", "RepoDigests", "repo_digests", "repoDigests")
            if key in observed
        },
    }
    return summary


def _manifest_value(
    path: Path,
    *,
    expected_hash: str,
    release_sha: str,
    integrity_mode: str = "unsigned",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    value = read_json(require_absolute(path, name="bundle manifest"), name="bundle manifest")
    if not isinstance(value, dict) or value.get("schema") not in {"runtime-bundle-manifest-v1", "bundle-manifest-v1"}:
        raise AdapterError("bundle manifest schema is invalid")
    if value.get("integrity_mode") != integrity_mode:
        raise AdapterError(f"bundle manifest integrity mode is not {integrity_mode}")
    if value.get("release_sha") != release_sha:
        raise AdapterError("bundle manifest release SHA does not match")
    if hashlib.sha256(canonical_json(value)).hexdigest() != expected_hash:
        raise AdapterError("bundle manifest hash does not match")
    members = value.get("members")
    if not isinstance(members, list) or not members:
        raise AdapterError("bundle manifest members are invalid")
    normalized = []
    seen: set[str] = set()
    for item in members:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
            raise AdapterError("bundle manifest member is invalid")
        path_value = item["path"]
        pure = PurePosixPath(path_value)
        if not path_value or "\\" in path_value or "\x00" in path_value or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise AdapterError("bundle manifest member path is unsafe")
        if path_value in seen:
            raise AdapterError("bundle manifest contains duplicate members")
        seen.add(path_value)
        digest = item["sha256"]
        if len(digest) != 64 or digest.lower() != digest or any(char not in "0123456789abcdef" for char in digest):
            raise AdapterError("bundle manifest member hash is invalid")
        install_root = item.get("install_root", "runtime")
        if install_root not in {"runtime", "systemd"}:
            raise AdapterError("bundle manifest install root is invalid")
        if install_root == "systemd":
            if not path_value.startswith("systemd/"):
                raise AdapterError("bundle manifest systemd path is invalid")
            target = path_value.removeprefix("systemd/")
            expected_mode = 0o644
        else:
            target = path_value
            if target not in EXPECTED_RUNTIME_MODES:
                raise AdapterError("bundle manifest runtime path is not closed")
            expected_mode = EXPECTED_RUNTIME_MODES[target]
        raw_mode = item.get("mode")
        try:
            mode = int(raw_mode, 8) if isinstance(raw_mode, str) else int(raw_mode)
        except (TypeError, ValueError) as exc:
            raise AdapterError("bundle manifest member mode is invalid") from exc
        if mode != expected_mode:
            raise AdapterError(f"bundle manifest member mode is not approved: {path_value}")
        normalized.append({"path": path_value, "target": target, "install_root": install_root, "mode": mode, "sha256": digest})
    expected_paths = {item["path"] for item in normalized}
    if expected_paths != EXPECTED_INSTALLED_RUNTIME_MEMBERS | EXPECTED_SYSTEMD_MEMBERS:
        raise AdapterError("bundle manifest members do not match the closed inventory")
    return value, normalized


def _bundle_evidence(args: argparse.Namespace, runtime_artifacts: Path, expected_hash: str, release_sha: str, *, production: bool) -> dict[str, Any]:
    if not production and args.bundle_receipt is None:
        evidence = _collect_tree(
            runtime_artifacts,
            label="runtime bundle",
            allow_unobservable_mount=not production,
        )
        if args.bundle_manifest is not None:
            manifest = read_json(require_absolute(args.bundle_manifest, name="bundle manifest"), name="bundle manifest")
            if not isinstance(manifest, dict):
                raise AdapterError("bundle manifest is not an object")
            if manifest.get("release_sha") not in (None, release_sha):
                raise AdapterError("bundle manifest release SHA does not match the caller")
            if hashlib.sha256(canonical_json(manifest)).hexdigest() != expected_hash:
                raise AdapterError("bundle manifest hash does not match the caller")
            evidence["manifest"] = os.fspath(args.bundle_manifest)
        elif evidence["tree_sha256"] != expected_hash:
            raise AdapterError("runtime bundle tree hash does not match the caller")
        evidence["bundle_sha256"] = expected_hash
        return evidence
    if args.bundle_receipt is None:
        raise AdapterError("production rollout requires an installed bundle receipt")
    receipt = read_json(require_absolute(args.bundle_receipt, name="bundle receipt"), name="bundle receipt")
    if not isinstance(receipt, dict) or receipt.get("schema") != "runtime-bundle-install-receipt-v1":
        raise AdapterError("bundle receipt is not a production install receipt")
    if receipt.get("dry_run") or receipt.get("evidence_eligible") is not True:
        raise AdapterError("dry-run or test-seam bundle receipt cannot be rollout evidence")
    if receipt.get("record_authority") != "non-authoritative-install-record-v1":
        raise AdapterError("bundle receipt is not a non-authoritative install record")
    if receipt.get("release_sha") != release_sha or receipt.get("manifest_sha256") != expected_hash:
        raise AdapterError("bundle receipt binding does not match")
    approval = receipt.get("approval")
    if approval is not None and not isinstance(approval, dict):
        raise AdapterError("bundle receipt record is malformed")
    target_root_value = receipt.get("target_root")
    if not isinstance(target_root_value, str) or Path(target_root_value) != runtime_artifacts:
        raise AdapterError("bundle receipt target is not the observed runtime root")
    if production and receipt.get("systemd_root") != os.fspath(PRODUCTION_SYSTEMD_ROOT):
        raise AdapterError("bundle receipt systemd root is not fixed")
    members = receipt.get("members")
    if not isinstance(members, list) or not all(isinstance(item, dict) for item in members):
        raise AdapterError("bundle receipt members are invalid")
    runtime_members = [item for item in members if item.get("install_root", "runtime") == "runtime"]
    systemd_members = [item for item in members if item.get("install_root") == "systemd"]
    if {item.get("target", item.get("path")) for item in runtime_members} != EXPECTED_INSTALLED_RUNTIME_MEMBERS:
        raise AdapterError("bundle receipt inventory does not match the closed runtime inventory")
    if {item.get("path") for item in systemd_members} != EXPECTED_SYSTEMD_MEMBERS:
        raise AdapterError("bundle receipt systemd inventory does not match the expected inventory")
    tree = _collect_tree(
        runtime_artifacts,
        label="installed runtime artifacts",
        exact_files=EXPECTED_INSTALLED_RUNTIME_MEMBERS,
        exact_dirs=frozenset(),
        allow_unobservable_mount=not production,
    )
    if production:
        if receipt.get("target_identity") != tree["root_identity"]:
            raise AdapterError("installed runtime root identity is not bound to the install record")
        root_stat = os.lstat(runtime_artifacts)
        if stat.S_IMODE(root_stat.st_mode) != 0o755 or root_stat.st_uid != 1000 or root_stat.st_gid != 1000:
            raise AdapterError("installed runtime root metadata is not approved")
        if any(entry["uid"] != 1000 or entry["gid"] != 1000 for entry in tree["entries"]):
            raise AdapterError("installed runtime member ownership is not approved")
    validated_runtime_members = runtime_members
    if production and args.bundle_manifest is not None:
        _, manifest_members = _manifest_value(
            args.bundle_manifest,
            expected_hash=expected_hash,
            release_sha=release_sha,
            integrity_mode="unsigned",
        )
        validated_runtime_members = [item for item in manifest_members if item.get("install_root", "runtime") == "runtime"]
    expected_by_path = {item.get("target", item.get("path")): item for item in validated_runtime_members}
    for entry in tree["entries"]:
        expected = expected_by_path.get(entry["path"])
        if expected is None:
            raise AdapterError(f"installed runtime member is not bound to the receipt: {entry['path']}")
        expected_mode = expected.get("mode")
        try:
            expected_mode = int(expected_mode, 8) if isinstance(expected_mode, str) else int(expected_mode)
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"installed runtime member mode is invalid: {entry['path']}") from exc
        if expected.get("sha256") != entry["sha256"] or expected_mode != entry["mode"]:
            raise AdapterError(f"installed runtime member is not bound to the receipt: {entry['path']}")
    if args.bundle_manifest is not None:
        _, manifest_members = _manifest_value(
            args.bundle_manifest,
            expected_hash=expected_hash,
            release_sha=release_sha,
            integrity_mode="unsigned",
        )

        def member_key(item: dict[str, Any]) -> tuple[str, str, int, str]:
            mode = item.get("mode")
            if isinstance(mode, str):
                try:
                    mode_value = int(mode, 8)
                except ValueError as exc:
                    raise AdapterError("bundle member mode is invalid") from exc
            elif isinstance(mode, int) and not isinstance(mode, bool):
                mode_value = mode
            else:
                raise AdapterError("bundle member mode is invalid")
            path = item.get("path")
            digest = item.get("sha256")
            install_root = item.get("install_root", "runtime")
            if not isinstance(path, str) or not isinstance(digest, str) or install_root not in {"runtime", "systemd"}:
                raise AdapterError("bundle member binding is invalid")
            return path, install_root, mode_value, digest

        if sorted(member_key(item) for item in manifest_members) != sorted(member_key(item) for item in members):
            raise AdapterError("installed bundle receipt members do not match the validated manifest")
    tree["bundle_sha256"] = expected_hash
    tree["source_receipt"] = os.fspath(args.bundle_receipt)
    tree["systemd_members"] = systemd_members
    return tree


def _systemd_evidence(
    root: Path,
    *,
    production: bool,
    expected_members: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    observed = _collect_tree(
        root,
        label="systemd artifacts",
        exact_files=None,
        exact_dirs=None,
        required_files=EXPECTED_SYSTEMD_FILES if production else None,
        required_dirs=EXPECTED_SYSTEMD_DIRECTORIES if production else None,
        tolerate_unrelated_unsafe=production,
        allow_unobservable_mount=not production,
    )
    if production:
        root_stat = os.lstat(root)
        if stat.S_IMODE(root_stat.st_mode) != 0o755 or root_stat.st_uid != 0 or root_stat.st_gid != 0:
            raise AdapterError("systemd root metadata is not approved")
        observed_dirs = {entry["path"]: entry for entry in observed["directories"]}
        for directory in EXPECTED_SYSTEMD_DIRECTORIES:
            entry = observed_dirs.get(directory)
            if entry is None or entry["mode"] != 0o755 or entry["uid"] != 0 or entry["gid"] != 0:
                raise AdapterError(f"systemd directory metadata mismatch: {directory}")
        expected_files = EXPECTED_SYSTEMD_FILES
        observed_by_path = {entry["path"]: entry for entry in observed["entries"]}
        for path in expected_files:
            entry = observed_by_path.get(path)
            if entry is None or entry["mode"] != 0o644 or entry["uid"] != 0 or entry["gid"] != 0:
                raise AdapterError(f"systemd artifact metadata mismatch: {path}")
        if expected_members is not None:
            for member in expected_members:
                if not isinstance(member, dict):
                    raise AdapterError("validated systemd member is invalid")
                raw_path = member.get("path")
                target = member.get("target")
                if not isinstance(target, str):
                    if not isinstance(raw_path, str) or not raw_path.startswith("systemd/"):
                        raise AdapterError("validated systemd member path is invalid")
                    target = raw_path.removeprefix("systemd/")
                entry = observed_by_path.get(target)
                if entry is None or entry["sha256"] != member.get("sha256"):
                    raise AdapterError(f"systemd artifact is not bound to the validated member: {target}")
                raw_mode = member.get("mode")
                try:
                    expected_mode = int(raw_mode, 8) if isinstance(raw_mode, str) else int(raw_mode)
                except (TypeError, ValueError) as exc:
                    raise AdapterError("validated systemd member mode is invalid") from exc
                if expected_mode != entry["mode"]:
                    raise AdapterError(f"systemd artifact mode is not bound: {target}")
    return observed


def _state_evidence(root: Path, *, production: bool) -> dict[str, Any]:
    evidence = _collect_tree(
        root,
        label="deployment state root",
        exact_files=EXPECTED_STATE_FILES if production else None,
        exact_dirs=EXPECTED_STATE_DIRECTORIES if production else None,
        allow_unobservable_mount=not production,
    )
    if production:
        root_stat = os.lstat(root)
        if stat.S_IMODE(root_stat.st_mode) != 0o700 or root_stat.st_uid != 1000 or root_stat.st_gid != 1000:
            raise AdapterError("deployment state root metadata is not approved")
        for entry in evidence["directories"]:
            if entry["path"] in EXPECTED_STATE_DIRECTORIES and (
                entry["mode"] != 0o700 or entry["uid"] != 1000 or entry["gid"] != 1000
            ):
                raise AdapterError(f"deployment state directory metadata mismatch: {entry['path']}")
        for entry in evidence["entries"]:
            if entry["mode"] != 0o600 or entry["uid"] != 1000 or entry["gid"] != 1000:
                raise AdapterError(f"deployment state member metadata mismatch: {entry['path']}")
        ensure_directory_metadata(
            PRODUCTION_EVIDENCE_ROOT,
            mode=0o700,
            uid=1000,
            gid=1000,
            name="StateFs evidence root",
        )
        ensure_regular_file(
            PRODUCTION_STATEFS_RECEIPT,
            mode=0o600,
            uid=1000,
            gid=1000,
            name="StateFs provision receipt",
        )
        statefs_receipt = read_json(PRODUCTION_STATEFS_RECEIPT, name="StateFs provision receipt")
        if not isinstance(statefs_receipt, dict) or statefs_receipt.get("schema") != "statefs-provision-receipt-v1":
            raise AdapterError("StateFs provision receipt has an unsupported schema")
        lock_proof = statefs_receipt.get("lock_proof")
        if statefs_receipt.get("evidence_eligible") is not True or not isinstance(lock_proof, dict) or lock_proof.get("held") is not True:
            raise AdapterError("StateFs provision receipt is not evidence-eligible")
        if statefs_receipt.get("root_identity") != evidence["root_identity"]:
            raise AdapterError("StateFs receipt root identity does not match the observed root")
        root_filesystem = evidence["root_identity"].get("filesystem")
        if root_filesystem not in ALLOWED_STATEFS:
            raise AdapterError("StateFs filesystem type is not approved")
        evidence_identity = statefs_receipt.get("evidence_identity")
        if not isinstance(evidence_identity, dict) or evidence_identity.get("filesystem") not in ALLOWED_STATEFS:
            raise AdapterError("StateFs evidence filesystem identity is not approved")
        if evidence_identity != filesystem_identity(PRODUCTION_EVIDENCE_ROOT, name="StateFs evidence root"):
            raise AdapterError("StateFs receipt evidence root identity does not match")
        receipt_entries = statefs_receipt.get("entries")
        if not isinstance(receipt_entries, list):
            raise AdapterError("StateFs provision receipt entries are invalid")
        by_path = {item.get("path"): item for item in receipt_entries if isinstance(item, dict)}
        if len(by_path) != len(receipt_entries) or set(by_path) != {entry["path"] for entry in evidence["entries"]}:
            raise AdapterError("StateFs provision receipt entries are ambiguous")
        for entry in evidence["entries"]:
            recorded = by_path.get(entry["path"])
            if not isinstance(recorded, dict) or any(
                recorded.get(key) != entry[key]
                for key in ("path", "mode", "uid", "gid", "identity")
            ):
                raise AdapterError(f"StateFs receipt does not bind {entry['path']}")
    admission = root / "deployment-admission" / "admission.json"
    if not admission.is_file():
        if production:
            raise AdapterError("deployment admission evidence is missing")
        return evidence
    value = read_json(admission, name="deployment admission")
    if not isinstance(value, dict) or value.get("schema") != "admission-v1" or value.get("mode") not in {"running", "quiescing", "recovery_required"}:
        raise AdapterError("deployment admission evidence is invalid")
    evidence["admission"] = {"schema": value["schema"], "mode": value["mode"], "generation": value.get("generation")}
    return evidence


def _cgroup_evidence(root: Path, *, production: bool) -> dict[str, Any]:
    evidence = _collect_tree(
        root,
        label="cgroup root",
        exact_files=None,
        exact_dirs=None,
        required_files=EXPECTED_CGROUP_FILES if production else None,
        tolerate_unrelated_unsafe=production,
        allow_unobservable_mount=not production,
    )
    identity = evidence["root_identity"]
    controllers: set[str] = set()
    delegated: set[str] = set()
    process_ids: list[int] = []
    if production:
        if identity.get("filesystem") != "cgroup2":
            raise AdapterError("production cgroup evidence is not from a cgroup2 mount")
        try:
            controllers = set((root / "cgroup.controllers").read_text(encoding="ascii").split())
            delegated = set((root / "cgroup.subtree_control").read_text(encoding="ascii").split())
            process_ids = [int(value) for value in (root / "cgroup.procs").read_text(encoding="ascii").split() if value.isdigit()]
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise AdapterError("cannot read production cgroup controller evidence") from exc
        if not REQUIRED_CGROUP_CONTROLLERS <= controllers:
            raise AdapterError("production cgroup controllers are incomplete")
        if not REQUIRED_CGROUP_CONTROLLERS <= delegated:
            raise AdapterError("production cgroup delegation is incomplete")
        if not process_ids:
            raise AdapterError("production cgroup has no member process")
    return {
        "available": True,
        "root": evidence["root"],
        "root_identity": identity,
        "controllers": sorted(controllers),
        "delegated_controllers": sorted(delegated),
        "process_ids": process_ids,
        "files": [{"path": item["path"], "sha256": item["sha256"], "mode": item["mode"]} for item in evidence["entries"]],
    }


def _cgroup_membership(data: bytes) -> set[str]:
    paths: set[str] = set()
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise AdapterError("process cgroup evidence is not ASCII") from exc
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0":
            paths.add(fields[2] or "/")
    return paths


def _process_evidence(pid: int | None, *, production: bool, cgroup_root: Path | None = None) -> dict[str, Any]:
    if pid is None:
        if production:
            raise AdapterError("production rollout requires process evidence")
        return {"available": False, "reason": "test seam did not request process evidence"}
    if pid <= 0:
        raise AdapterError("process pid must be positive")
    proc_root = Path("/proc") / str(pid)
    if not proc_root.is_dir():
        raise AdapterError(f"process evidence is missing for pid {pid}")
    files = {}
    cmdline_data = b""
    cgroup_data = b""
    for name in ("stat", "status", "cgroup", "cmdline"):
        path = proc_root / name
        ensure_no_symlink(path, name="process evidence", allow_missing=False)
        try:
            data = path.read_bytes()
        except (OSError, UnicodeDecodeError) as exc:
            raise AdapterError("cannot read process evidence") from exc
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        if name == "cmdline":
            cmdline_data = data
        elif name == "cgroup":
            cgroup_data = data
    if production and (not cmdline_data or b"asrsub" not in cmdline_data.lower()):
        raise AdapterError("process evidence is not an ASRSub runtime")
    if production and files["cmdline"]["bytes"] == 0:
        raise AdapterError("process evidence has no command identity")
    membership = _cgroup_membership(cgroup_data)
    if production:
        if cgroup_root is None or cgroup_root != PRODUCTION_CGROUP_ROOT:
            raise AdapterError("production process cgroup root is not fixed")
        expected = "/" + cgroup_root.relative_to(Path("/sys/fs/cgroup")).as_posix()
        if expected not in membership:
            raise AdapterError("process is not a member of the fixed ASRSub runtime cgroup")
    return {"available": True, "pid": pid, "cgroup_paths": sorted(membership), "files": files}


def _health_evidence(path: Path | None, *, production: bool) -> dict[str, Any]:
    if path is None:
        if production:
            raise AdapterError("production rollout requires health evidence")
        return {"available": False, "reason": "test seam did not request health evidence"}
    path = require_absolute(path, name="health evidence")
    probe_evidence = None
    if production:
        if path != PRODUCTION_HEALTH_EVIDENCE:
            raise AdapterError("production health evidence path is not fixed")
        ensure_regular_file(
            PRODUCTION_HEALTH_PROBE,
            mode=0o755,
            uid=1000,
            gid=1000,
            name="installed health probe",
        )
        probe_evidence = run_argv(
            [PRODUCTION_HEALTH_PROBE, "--output", path],
            cwd=PRODUCTION_RUNTIME_ROOT,
            secret_values=environment_secret_values(),
            env=production_command_environment(),
        )
        ensure_regular_file(path, mode=0o600, uid=1000, gid=1000, name="health evidence")
    value = read_json(path, name="health evidence")
    if not isinstance(value, dict) or value.get("schema") != "health-evidence-v1":
        raise AdapterError("health evidence has an unsupported schema")
    if value.get("returncode") != 0 or value.get("status") != 200 or value.get("ready") is not True:
        raise AdapterError("health evidence is not a successful /ready observation")
    if value.get("endpoint") != "/ready":
        raise AdapterError("health evidence endpoint is not /ready")
    result = {"schema": value["schema"], "endpoint": "/ready", "status": 200, "returncode": 0, "ready": True}
    if probe_evidence is not None:
        result["probe"] = {
            "path": os.fspath(PRODUCTION_HEALTH_PROBE),
            "sha256": sha256_file(PRODUCTION_HEALTH_PROBE, name="installed health probe"),
            "argv": probe_evidence["argv"],
        }
    return result


def _approval_evidence(path: Path, *, release_sha: str, image_digest: str, bundle_sha: str, production: bool) -> dict[str, Any]:
    value = read_json(require_absolute(path, name="rollout approval"), name="rollout approval")
    if not isinstance(value, dict) or value.get("schema") != "approval-v1":
        raise AdapterError("rollout approval has an unsupported schema")
    if value.get("integrity_mode") != "unsigned":
        raise AdapterError("rollout approval integrity mode is not unsigned")
    if value.get("release_sha") != release_sha or value.get("bundle_sha256") != bundle_sha:
        raise AdapterError("rollout approval release or bundle binding does not match")
    if value.get("image_digest") != image_digest.rsplit(":", 1)[-1]:
        raise AdapterError("rollout approval image binding does not match")
    if value.get("approved_docker_socket") != "default" or value.get("approved_state_root") != os.fspath(PRODUCTION_STATE_ROOT):
        raise AdapterError("rollout approval host bindings are not approved")
    generation = value.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
        raise AdapterError("rollout approval generation is invalid")
    return {"schema": "approval-v1", "integrity_mode": "unsigned", "generation": value.get("generation"), "release_sha": release_sha, "bundle_sha256": bundle_sha, "image_ref": image_digest, "image_digest": image_digest.rsplit(":", 1)[-1], "approved_docker_socket": "default", "approved_state_root": os.fspath(PRODUCTION_STATE_ROOT)}


def _require_preflight(*, release_sha: str, image_digest: str) -> None:
    try:
        from production_entrypoint import preflight  # type: ignore

        approved = preflight()
    except (AdapterError, ImportError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("production rollout requires a successful preflight") from exc
    if not isinstance(approved, dict) or approved.get("release_sha") != release_sha or approved.get("image_digest") != image_digest:
        raise AdapterError("production rollout authorization does not match the observed release")


def _production(args: argparse.Namespace, *, test_seam: bool) -> int:
    required = {
        "deployment root": args.deployment_root,
        "runtime bundle": args.runtime_bundle,
        "Docker evidence": args.docker_evidence,
        "release SHA": args.release_sha,
        "image digest": args.image_digest,
        "bundle SHA256": args.bundle_sha256,
        "output": args.output,
    }
    for label, value in required.items():
        if value is None:
            raise AdapterError(f"{'test-seam' if test_seam else 'production'} mode requires {label}")
    release_sha = require_hex(args.release_sha, name="release SHA", length=40)
    image_digest = require_image_digest(args.image_digest)
    bundle_sha = require_hex(args.bundle_sha256, name="bundle SHA256", length=64)
    deployment_root = require_absolute(args.deployment_root, name="deployment root")
    runtime_bundle = require_absolute(args.runtime_bundle, name="runtime bundle")
    if not test_seam:
        if deployment_root != PRODUCTION_STATE_ROOT:
            raise AdapterError("production deployment root is not fixed")
        if runtime_bundle != PRODUCTION_RUNTIME_ROOT:
            raise AdapterError("production runtime bundle is not fixed")
        if args.runtime_artifacts is None or require_absolute(args.runtime_artifacts, name="runtime artifacts") != PRODUCTION_RUNTIME_ROOT:
            raise AdapterError("production requires the fixed installed runtime artifacts")
        if require_absolute(args.systemd_root, name="systemd root") != PRODUCTION_SYSTEMD_ROOT:
            raise AdapterError("production systemd root is not fixed")
        if require_absolute(args.cgroup_root, name="cgroup root") != PRODUCTION_CGROUP_ROOT:
            raise AdapterError("production cgroup root is not fixed")
        if require_absolute(args.docker_evidence, name="Docker evidence") != PRODUCTION_DOCKER_EVIDENCE:
            raise AdapterError("production Docker evidence path is not fixed")
        if args.health_evidence is None or require_absolute(args.health_evidence, name="health evidence") != PRODUCTION_HEALTH_EVIDENCE:
            raise AdapterError("production health evidence path is not fixed")
        if args.bundle_receipt is None or require_absolute(args.bundle_receipt, name="bundle receipt") != PRODUCTION_BUNDLE_RECEIPT:
            raise AdapterError("production bundle receipt path is not fixed")
        if args.bundle_manifest is None or require_absolute(args.bundle_manifest, name="bundle manifest") != PRODUCTION_BUNDLE_MANIFEST:
            raise AdapterError("production bundle manifest path is not fixed")
        if args.approval is None or require_absolute(args.approval, name="rollout approval") != PRODUCTION_APPROVAL:
            raise AdapterError("production approval path is not fixed")
        if require_absolute(args.output, name="rollout receipt output") != PRODUCTION_ROLLOUT_OUTPUT:
            raise AdapterError("production rollout output path is not fixed")
        _require_preflight(release_sha=release_sha, image_digest=image_digest)
    if args.collect_transaction_cgroup or args.collect_target_evidence:
        raise AdapterError("unused evidence collection flags are not accepted")

    deployment = _state_evidence(deployment_root, production=not test_seam)
    docker = _docker_evidence(args.docker_evidence, release_sha=release_sha, image_digest=image_digest, production=not test_seam)
    bundle = _bundle_evidence(args, runtime_bundle if test_seam and args.runtime_artifacts is None else require_absolute(args.runtime_artifacts or runtime_bundle, name="runtime artifacts"), bundle_sha, release_sha, production=not test_seam)
    systemd = _systemd_evidence(
        ensure_existing_directory(require_absolute(args.systemd_root, name="systemd root"), name="systemd root"),
        production=not test_seam,
        expected_members=bundle.get("systemd_members") if not test_seam else None,
    )
    runtime_artifacts_path = require_absolute(args.runtime_artifacts or runtime_bundle, name="runtime artifacts")
    if not test_seam and runtime_artifacts_path == runtime_bundle and runtime_artifacts_path != PRODUCTION_RUNTIME_ROOT:
        raise AdapterError("source bundle cannot stand in for installed runtime artifacts")
    cgroup = _cgroup_evidence(require_absolute(args.cgroup_root, name="cgroup root"), production=not test_seam)
    health = _health_evidence(args.health_evidence, production=not test_seam)
    process = _process_evidence(args.process_pid, production=not test_seam, cgroup_root=require_absolute(args.cgroup_root, name="cgroup root"))
    approval = None
    if not test_seam:
        approval = _approval_evidence(args.approval, release_sha=release_sha, image_digest=image_digest, bundle_sha=bundle_sha, production=True)

    receipt = {
        "schema": "rollout-receipt-v1",
        "kind": "test-seam" if test_seam else "production",
        "result": "success",
        "release_sha": release_sha,
        "image_ref": image_digest,
        "image_digest": image_digest.rsplit(":", 1)[-1],
        "bundle_sha256": bundle_sha,
        "approval": approval,
        "evidence": {
            "deployment_root": deployment,
            "runtime_bundle": bundle,
            "docker": docker,
            "systemd": systemd,
            "runtime_artifacts": bundle,
            "cgroup": cgroup,
            "health": health,
            "process": process,
        },
        "evidence_paths": [
            os.fspath(path)
            for path in (
                args.deployment_root,
                args.runtime_bundle,
                args.docker_evidence,
                args.systemd_root,
                args.cgroup_root,
                args.health_evidence,
            )
            if path is not None
        ],
        "evidence_eligible": not test_seam,
        "collected_epoch_ns": __import__("time").time_ns(),
    }
    output = require_absolute(args.output, name="rollout receipt output")
    ensure_parent_directory(output, name="rollout receipt output")
    uid, gid = default_owner()
    atomic_write_json(output, receipt, mode=0o600, uid=uid, gid=gid, name="rollout receipt")
    return 0


def _mode(args: argparse.Namespace) -> str:
    fixture = args.fixture is not None
    production = bool(args.production or args.mode == "production")
    seam = bool(args.test_seam or args.mode == "test-seam")
    if args.mode == "fixture" and (production or seam):
        raise AdapterError("fixture, production, and test-seam modes are mutually exclusive")
    if production and (fixture or seam):
        raise AdapterError("production, fixture, and test-seam modes are mutually exclusive")
    if seam and fixture:
        raise AdapterError("test-seam mode rejects --fixture")
    if not production and not fixture and not seam:
        raise AdapterError("select --production, --test-seam, or provide --fixture")
    return "production" if production else "test-seam" if seam else "fixture"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "production", "test-seam"))
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--test-seam", action="store_true")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--deployment-root", type=Path)
    parser.add_argument("--runtime-bundle", "--bundle-root", dest="runtime_bundle", type=Path)
    parser.add_argument("--runtime-artifacts", type=Path)
    parser.add_argument("--bundle-manifest", type=Path)
    parser.add_argument("--bundle-receipt", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--docker-evidence", "--docker-metadata", dest="docker_evidence", type=Path)
    parser.add_argument("--health-evidence", type=Path)
    parser.add_argument("--systemd-root", type=Path, default=Path("/etc/systemd/system"))
    parser.add_argument("--cgroup-root", type=Path, default=Path("/sys/fs/cgroup"))
    parser.add_argument("--process-pid", type=int)
    parser.add_argument("--release-sha")
    parser.add_argument("--image-digest", "--digest", dest="image_digest")
    parser.add_argument("--bundle-sha256", "--bundle-hash", dest="bundle_sha256")
    parser.add_argument("--collect-transaction-cgroup", action="store_true")
    parser.add_argument("--collect-target-evidence", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        mode = _mode(args)
        return _fixture(args) if mode == "fixture" else _production(args, test_seam=mode == "test-seam")
    except (AdapterError, OSError, UnicodeDecodeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(redact_text(str(exc), secret_values=environment_secret_values()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
