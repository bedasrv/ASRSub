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
from pathlib import Path
from typing import Any, Iterable

from production_adapter_common import (
    AdapterError,
    atomic_write_json,
    canonical_json,
    default_owner,
    ensure_existing_directory,
    ensure_no_symlink,
    ensure_parent_directory,
    environment_secret_values,
    filesystem_identity,
    read_json,
    redact_text,
    require_absolute,
    require_hex,
    require_image_digest,
    sha256_file,
)
from deploy_docker import COMPOSE_FILE, DOCKER, PROJECT_DIRECTORY, docker_argv


PRODUCTION_STATE_ROOT = Path("/var/lib/asrsub/state")
PRODUCTION_RUNTIME_ROOT = Path("/usr/local/libexec/asrsub")
PRODUCTION_SYSTEMD_ROOT = Path("/etc/systemd/system")
PRODUCTION_CGROUP_ROOT = Path("/sys/fs/cgroup/system.slice/asrsub-runtime.service/asrsub-children")
PRODUCTION_EVIDENCE_ROOT = Path("/var/lib/asrsub/deploy-state/evidence")
PRODUCTION_ROLLOUT_OUTPUT = PRODUCTION_EVIDENCE_ROOT / "rollout.json"
PRODUCTION_DOCKER_EVIDENCE = PRODUCTION_EVIDENCE_ROOT / "image-inspect.json"
PRODUCTION_HEALTH_EVIDENCE = PRODUCTION_EVIDENCE_ROOT / "health.json"
PRODUCTION_BUNDLE_RECEIPT = PRODUCTION_EVIDENCE_ROOT / "runtime-bundle-install.json"
PRODUCTION_BUNDLE_MANIFEST = Path("/var/lib/asrsub/deploy-state/bundle-manifest.json")
PRODUCTION_APPROVAL = Path("/var/lib/asrsub/deploy-state/approval.json")
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
EXPECTED_CGROUP_FILES = frozenset({"cgroup.controllers", "cgroup.procs"})


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
) -> dict[str, Any]:
    root = ensure_existing_directory(require_absolute(root, name=label), name=label)
    entries: list[dict[str, Any]] = []
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
                raise AdapterError(f"{label} contains an unsafe directory")
            directories.add(path.relative_to(root).as_posix())
        for name in files:
            path = current_path / name
            try:
                st = os.lstat(path)
            except OSError as exc:
                raise AdapterError(f"cannot inspect {label}") from exc
            if stat.S_ISLNK(st.st_mode):
                raise AdapterError(f"{label} contains a symlink")
            if not stat.S_ISREG(st.st_mode):
                raise AdapterError(f"{label} contains an unsupported entry")
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "mode": stat.S_IMODE(st.st_mode),
                    "uid": st.st_uid,
                    "gid": st.st_gid,
                    "sha256": sha256_file(path, name=f"{label} member"),
                    "identity": filesystem_identity(path, name=f"{label} member"),
                }
            )
    entries.sort(key=lambda entry: entry["path"])
    observed_files = {entry["path"] for entry in entries}
    if exact_files is not None and observed_files != set(exact_files):
        missing = sorted(set(exact_files) - observed_files)
        extra = sorted(observed_files - set(exact_files))
        raise AdapterError(f"{label} inventory mismatch: missing={missing}, extra={extra}")
    if exact_dirs is not None and directories != set(exact_dirs):
        missing = sorted(set(exact_dirs) - directories)
        extra = sorted(directories - set(exact_dirs))
        raise AdapterError(f"{label} directory inventory mismatch: missing={missing}, extra={extra}")
    if not entries:
        raise AdapterError(f"{label} has no observable regular files")
    return {
        "root": os.fspath(root),
        "root_identity": filesystem_identity(root, name=label),
        "observed": True,
        "entries": entries,
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


def _manifest_value(path: Path, *, expected_hash: str, release_sha: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    value = read_json(require_absolute(path, name="bundle manifest"), name="bundle manifest")
    if not isinstance(value, dict) or value.get("release_sha") != release_sha:
        raise AdapterError("bundle manifest release SHA does not match")
    if hashlib.sha256(canonical_json(value)).hexdigest() != expected_hash:
        raise AdapterError("bundle manifest hash does not match")
    members = value.get("members")
    if not isinstance(members, list) or {item.get("path") for item in members if isinstance(item, dict)} != EXPECTED_RUNTIME_MEMBERS:
        raise AdapterError("bundle manifest members do not match the closed inventory")
    normalized = []
    for item in members:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
            raise AdapterError("bundle manifest member is invalid")
        normalized.append({"path": item["path"], "mode": item.get("mode"), "sha256": item["sha256"]})
    return value, normalized


def _bundle_evidence(args: argparse.Namespace, runtime_artifacts: Path, expected_hash: str, release_sha: str, *, production: bool) -> dict[str, Any]:
    if not production and args.bundle_receipt is None:
        evidence = _collect_tree(runtime_artifacts, label="runtime bundle")
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
    if receipt.get("release_sha") != release_sha or receipt.get("manifest_sha256") != expected_hash:
        raise AdapterError("bundle receipt binding does not match")
    approval = receipt.get("approval")
    if not isinstance(approval, dict) or approval.get("verified") is not True:
        raise AdapterError("bundle receipt does not carry authenticated approval")
    target_root_value = receipt.get("target_root")
    if not isinstance(target_root_value, str) or Path(target_root_value) != runtime_artifacts:
        raise AdapterError("bundle receipt target is not the observed runtime root")
    members = receipt.get("members")
    if not isinstance(members, list) or not all(isinstance(item, dict) for item in members) or {item.get("path") for item in members} != EXPECTED_RUNTIME_MEMBERS:
        raise AdapterError("bundle receipt inventory does not match the closed runtime inventory")
    tree = _collect_tree(
        runtime_artifacts,
        label="installed runtime artifacts",
        exact_files=EXPECTED_RUNTIME_MEMBERS,
        exact_dirs=frozenset(),
    )
    if production:
        root_stat = os.lstat(runtime_artifacts)
        if stat.S_IMODE(root_stat.st_mode) != 0o755 or root_stat.st_uid != 1000 or root_stat.st_gid != 1000:
            raise AdapterError("installed runtime root metadata is not approved")
        if any(entry["uid"] != 1000 or entry["gid"] != 1000 for entry in tree["entries"]):
            raise AdapterError("installed runtime member ownership is not approved")
    expected_by_path = {item["path"]: item for item in members}
    for entry in tree["entries"]:
        expected = expected_by_path.get(entry["path"])
        if expected is None:
            raise AdapterError(f"installed runtime member is not bound to the receipt: {entry['path']}")
        expected_mode = expected.get("mode")
        try:
            expected_mode = int(expected_mode, 8) if isinstance(expected_mode, str) else int(expected_mode)
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"installed runtime member mode is invalid: {entry['path']}") from exc
        if expected is None or expected.get("sha256") != entry["sha256"] or expected_mode != entry["mode"]:
            raise AdapterError(f"installed runtime member is not bound to the receipt: {entry['path']}")
    if args.bundle_manifest is not None:
        _, manifest_members = _manifest_value(args.bundle_manifest, expected_hash=expected_hash, release_sha=release_sha)
        def member_key(item: dict[str, Any]) -> tuple[str, int, str]:
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
            if not isinstance(path, str) or not isinstance(digest, str):
                raise AdapterError("bundle member binding is invalid")
            return path, mode_value, digest
        if sorted(member_key(item) for item in manifest_members) != sorted(member_key(item) for item in members):
            raise AdapterError("installed bundle receipt members do not match the signed manifest")
    tree["bundle_sha256"] = expected_hash
    tree["source_receipt"] = os.fspath(args.bundle_receipt)
    return tree


def _systemd_evidence(root: Path, *, production: bool) -> dict[str, Any]:
    observed = _collect_tree(
        root,
        label="systemd artifacts",
        exact_files=EXPECTED_SYSTEMD_FILES if production else None,
        exact_dirs=EXPECTED_SYSTEMD_DIRECTORIES if production else None,
    )
    if production:
        source_root = Path(__file__).resolve().parents[1] / "systemd"
        for relative in EXPECTED_SYSTEMD_FILES:
            target = root / relative
            source = source_root / relative
            if not source.is_file() or target.read_bytes() != source.read_bytes():
                raise AdapterError(f"systemd artifact content mismatch: {relative}")
    return observed


def _state_evidence(root: Path, *, production: bool) -> dict[str, Any]:
    evidence = _collect_tree(
        root,
        label="deployment state root",
        exact_files=EXPECTED_STATE_FILES if production else None,
        exact_dirs=EXPECTED_STATE_DIRECTORIES if production else None,
    )
    if production:
        root_stat = os.lstat(root)
        if stat.S_IMODE(root_stat.st_mode) != 0o700 or root_stat.st_uid != 1000 or root_stat.st_gid != 1000:
            raise AdapterError("deployment state root metadata is not approved")
        for entry in evidence["entries"]:
            expected_mode = 0o700 if entry["path"] in EXPECTED_STATE_DIRECTORIES else 0o600
            if entry["mode"] != expected_mode or entry["uid"] != 1000 or entry["gid"] != 1000:
                raise AdapterError(f"deployment state member metadata mismatch: {entry['path']}")
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
        exact_files=EXPECTED_CGROUP_FILES if production else None,
        exact_dirs=frozenset() if production else None,
    )
    return {
        "available": True,
        "root": evidence["root"],
        "root_identity": evidence["root_identity"],
        "files": [{"path": item["path"], "sha256": item["sha256"], "mode": item["mode"]} for item in evidence["entries"]],
    }


def _process_evidence(pid: int | None, *, production: bool) -> dict[str, Any]:
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
    if production and (not cmdline_data or b"asrsub" not in cmdline_data.lower()):
        raise AdapterError("process evidence is not an ASRSub runtime")
    if production and files["cmdline"]["bytes"] == 0:
        raise AdapterError("process evidence has no command identity")
    return {"available": True, "pid": pid, "files": files}


def _health_evidence(path: Path | None, *, production: bool) -> dict[str, Any]:
    if path is None:
        if production:
            raise AdapterError("production rollout requires health evidence")
        return {"available": False, "reason": "test seam did not request health evidence"}
    value = read_json(require_absolute(path, name="health evidence"), name="health evidence")
    if not isinstance(value, dict) or value.get("schema") != "health-evidence-v1":
        raise AdapterError("health evidence has an unsupported schema")
    if value.get("returncode") != 0 or value.get("status") != 200 or value.get("ready") is not True:
        raise AdapterError("health evidence is not a successful /ready observation")
    if production and value.get("endpoint") != "/ready":
        raise AdapterError("health evidence endpoint is not /ready")
    return {"schema": value["schema"], "endpoint": "/ready", "status": 200, "returncode": 0, "ready": True}


def _approval_evidence(path: Path, *, release_sha: str, image_digest: str, bundle_sha: str, production: bool) -> dict[str, Any]:
    value = read_json(require_absolute(path, name="rollout approval"), name="rollout approval")
    if not isinstance(value, dict) or value.get("schema") != "approval-v1":
        raise AdapterError("rollout approval has an unsupported schema")
    if value.get("release_sha") != release_sha or value.get("bundle_sha256") != bundle_sha:
        raise AdapterError("rollout approval release or bundle binding does not match")
    if value.get("image_digest") != image_digest.rsplit(":", 1)[-1]:
        raise AdapterError("rollout approval image binding does not match")
    if value.get("approved_docker_socket") != "default" or value.get("approved_state_root") != os.fspath(PRODUCTION_STATE_ROOT):
        raise AdapterError("rollout approval host bindings are not approved")
    generation = value.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
        raise AdapterError("rollout approval generation is invalid")
    return {"schema": "approval-v1", "generation": value.get("generation"), "release_sha": release_sha, "bundle_sha256": bundle_sha, "image_digest": image_digest, "approved_docker_socket": "default", "approved_state_root": os.fspath(PRODUCTION_STATE_ROOT)}


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
    if args.collect_transaction_cgroup or args.collect_target_evidence:
        raise AdapterError("unused evidence collection flags are not accepted")

    deployment = _state_evidence(deployment_root, production=not test_seam)
    docker = _docker_evidence(args.docker_evidence, release_sha=release_sha, image_digest=image_digest, production=not test_seam)
    bundle = _bundle_evidence(args, runtime_bundle if test_seam and args.runtime_artifacts is None else require_absolute(args.runtime_artifacts or runtime_bundle, name="runtime artifacts"), bundle_sha, release_sha, production=not test_seam)
    systemd = _systemd_evidence(ensure_existing_directory(require_absolute(args.systemd_root, name="systemd root"), name="systemd root"), production=not test_seam)
    runtime_artifacts_path = require_absolute(args.runtime_artifacts or runtime_bundle, name="runtime artifacts")
    if not test_seam and runtime_artifacts_path == runtime_bundle and runtime_artifacts_path != PRODUCTION_RUNTIME_ROOT:
        raise AdapterError("source bundle cannot stand in for installed runtime artifacts")
    cgroup = _cgroup_evidence(require_absolute(args.cgroup_root, name="cgroup root"), production=not test_seam)
    health = _health_evidence(args.health_evidence, production=not test_seam)
    process = _process_evidence(args.process_pid, production=not test_seam)
    approval = None
    if not test_seam:
        approval = _approval_evidence(args.approval, release_sha=release_sha, image_digest=image_digest, bundle_sha=bundle_sha, production=True)

    receipt = {
        "schema": "rollout-receipt-v1",
        "kind": "test-seam" if test_seam else "production",
        "result": "success",
        "release_sha": release_sha,
        "image_digest": image_digest,
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
