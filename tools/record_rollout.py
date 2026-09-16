#!/usr/bin/env python3
"""Collect local rollout evidence without claiming observations not made."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
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
    filesystem_identity,
    now_ns,
    read_json,
    redact_text,
    require_absolute,
    require_hex,
    require_image_digest,
    sha256_file,
)


def _fixture(args: argparse.Namespace) -> int:
    if args.fixture is None or args.output is None:
        raise AdapterError("fixture mode requires --fixture and --output")
    value = read_json(require_absolute(args.fixture, name="rollout fixture"), name="rollout fixture")
    if not isinstance(value, dict):
        raise AdapterError("rollout fixture must be an object")
    receipt = {"schema": "rollout-receipt-v1", "kind": "fixture", "result": "success", "evidence_paths": []}
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


def _collect_tree(root: Path, *, label: str) -> dict[str, Any]:
    root = ensure_existing_directory(require_absolute(root, name=label), name=label)
    entries: list[dict[str, Any]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *files):
            path = current_path / name
            try:
                st = os.lstat(path)
            except OSError as exc:
                raise AdapterError(f"cannot inspect {label}: {exc.strerror or exc}") from exc
            if stat.S_ISLNK(st.st_mode):
                raise AdapterError(f"{label} contains a symlink: {path}")
            if stat.S_ISDIR(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                raise AdapterError(f"{label} contains an unsupported entry: {path}")
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "mode": stat.S_IMODE(st.st_mode),
                    "sha256": sha256_file(path, name=f"{label} member"),
                    "identity": filesystem_identity(path, name=f"{label} member"),
                }
            )
    entries.sort(key=lambda entry: entry["path"])
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


def _docker_evidence(path: Path, *, release_sha: str, image_digest: str) -> dict[str, Any]:
    value = read_json(require_absolute(path, name="Docker evidence"), name="Docker evidence")
    if not isinstance(value, dict) or value.get("schema") != "docker-operation-evidence-v1":
        raise AdapterError("Docker evidence has an unsupported schema")
    if value.get("requested_digest") not in (None, image_digest):
        raise AdapterError("Docker evidence requested digest does not match the caller")
    if value.get("image_digest") not in (None, image_digest):
        raise AdapterError("Docker evidence image digest does not match the caller")
    if "observed" not in value or not _contains_digest(value["observed"], image_digest):
        raise AdapterError("Docker evidence does not prove the requested image digest")
    if value.get("release_sha") not in (None, release_sha):
        raise AdapterError("Docker evidence release SHA does not match the caller")
    argv = value.get("argv", [])
    if not isinstance(argv, list):
        raise AdapterError("Docker evidence argv is invalid")
    observed = value["observed"]
    if not isinstance(observed, dict):
        raise AdapterError("Docker evidence observed object is invalid")
    summary: dict[str, Any] = {
        "schema": value["schema"],
        "operation": value.get("operation"),
        "requested_digest": image_digest,
        "image_digest": image_digest,
        "release_sha": release_sha,
        "argv": [redact_text(str(item)) for item in argv],
        "observed": {
            key: observed[key]
            for key in ("image_ref", "image_digest", "Id", "id", "RepoDigests", "repo_digests", "repoDigests")
            if key in observed
        },
        "stdout": redact_text(str(value.get("stdout", ""))),
        "stderr": redact_text(str(value.get("stderr", ""))),
    }
    return summary


def _bundle_evidence(args: argparse.Namespace, runtime_bundle: Path, expected_hash: str, release_sha: str) -> dict[str, Any]:
    evidence = _collect_tree(runtime_bundle, label="runtime bundle")
    manifest_path = args.bundle_manifest
    if manifest_path is None:
        candidate = runtime_bundle / "manifest.json"
        if candidate.is_file() and not candidate.is_symlink():
            manifest_path = candidate
    if args.bundle_receipt is not None:
        receipt = read_json(require_absolute(args.bundle_receipt, name="bundle receipt"), name="bundle receipt")
        if not isinstance(receipt, dict) or receipt.get("manifest_sha256") != expected_hash:
            raise AdapterError("bundle receipt hash does not match the caller")
        if receipt.get("release_sha") not in (None, release_sha):
            raise AdapterError("bundle receipt release SHA does not match the caller")
        evidence["source_receipt"] = os.fspath(args.bundle_receipt)
    elif manifest_path is not None:
        manifest = read_json(require_absolute(manifest_path, name="bundle manifest"), name="bundle manifest")
        if not isinstance(manifest, dict):
            raise AdapterError("bundle manifest is not an object")
        if manifest.get("release_sha") not in (None, release_sha):
            raise AdapterError("bundle manifest release SHA does not match the caller")
        actual_hash = hashlib.sha256(canonical_json(manifest)).hexdigest()
        if actual_hash != expected_hash:
            raise AdapterError("bundle manifest hash does not match the caller")
        evidence["manifest"] = os.fspath(manifest_path)
    else:
        actual_hash = evidence["tree_sha256"]
        if actual_hash != expected_hash:
            raise AdapterError("runtime bundle tree hash does not match the caller")
    evidence["bundle_sha256"] = expected_hash
    return evidence


def _cgroup_evidence(root: Path) -> dict[str, Any]:
    evidence = _collect_tree(root, label="cgroup root")
    return {
        "available": True,
        "root": evidence["root"],
        "root_identity": evidence["root_identity"],
        "files": [{"path": item["path"], "sha256": item["sha256"], "mode": item["mode"]} for item in evidence["entries"]],
    }


def _process_evidence(pid: int | None) -> dict[str, Any]:
    if pid is None:
        return {"available": False, "reason": "not requested"}
    if pid <= 0:
        raise AdapterError("process pid must be positive")
    proc_root = Path("/proc") / str(pid)
    if not proc_root.is_dir():
        raise AdapterError(f"process evidence is missing for pid {pid}")
    files = {}
    for name in ("stat", "status", "cgroup"):
        path = proc_root / name
        ensure_no_symlink(path, name="process evidence", allow_missing=False)
        data = path.read_bytes()
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    return {"available": True, "pid": pid, "files": files}


def _production(args: argparse.Namespace) -> int:
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
            raise AdapterError(f"production mode requires {label}")
    release_sha = require_hex(args.release_sha, name="release SHA", length=40)
    image_digest = require_image_digest(args.image_digest)
    bundle_sha = require_hex(args.bundle_sha256, name="bundle SHA256", length=64)
    deployment = _collect_tree(args.deployment_root, label="deployment root")
    runtime_bundle = require_absolute(args.runtime_bundle, name="runtime bundle")
    docker = _docker_evidence(args.docker_evidence, release_sha=release_sha, image_digest=image_digest)
    bundle = _bundle_evidence(args, runtime_bundle, bundle_sha, release_sha)
    systemd_root = ensure_existing_directory(require_absolute(args.systemd_root, name="systemd root"), name="systemd root")
    systemd = _collect_tree(systemd_root, label="systemd artifacts")
    required_units = {"asrsub-recovery.service", "asrsub-runtime.service"}
    observed_units = {entry["path"] for entry in systemd["entries"]}
    missing_units = sorted(required_units - observed_units)
    if missing_units:
        raise AdapterError("systemd evidence is missing: " + ", ".join(missing_units))
    runtime_artifacts_path = args.runtime_artifacts or runtime_bundle
    runtime_artifacts = _collect_tree(runtime_artifacts_path, label="runtime artifacts")
    cgroup = _cgroup_evidence(require_absolute(args.cgroup_root, name="cgroup root"))
    process = _process_evidence(args.process_pid)
    receipt = {
        "schema": "rollout-receipt-v1",
        "kind": "production",
        "result": "success",
        "release_sha": release_sha,
        "image_digest": image_digest,
        "bundle_sha256": bundle_sha,
        "evidence": {
            "deployment_root": deployment,
            "runtime_bundle": bundle,
            "docker": docker,
            "systemd": systemd,
            "runtime_artifacts": runtime_artifacts,
            "cgroup": cgroup,
            "process": process,
        },
        "evidence_paths": [
            os.fspath(args.deployment_root),
            os.fspath(args.runtime_bundle),
            os.fspath(args.docker_evidence),
            os.fspath(args.systemd_root),
            os.fspath(args.cgroup_root),
        ],
        "collected_epoch_ns": now_ns(),
    }
    output = require_absolute(args.output, name="rollout receipt output")
    ensure_parent_directory(output, name="rollout receipt output")
    uid, gid = default_owner()
    atomic_write_json(output, receipt, mode=0o600, uid=uid, gid=gid, name="rollout receipt")
    return 0


def _mode(args: argparse.Namespace) -> str:
    fixture = args.fixture is not None
    production = bool(args.production or args.mode == "production")
    if args.mode == "fixture" and production:
        raise AdapterError("fixture and production modes are mutually exclusive")
    if production and fixture:
        raise AdapterError("production mode rejects --fixture")
    if not production and not fixture:
        raise AdapterError("select --production explicitly or provide --fixture")
    return "production" if production else "fixture"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "production"))
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--deployment-root", type=Path)
    parser.add_argument("--runtime-bundle", "--bundle-root", dest="runtime_bundle", type=Path)
    parser.add_argument("--bundle-manifest", type=Path)
    parser.add_argument("--bundle-receipt", type=Path)
    parser.add_argument("--docker-evidence", "--docker-metadata", dest="docker_evidence", type=Path)
    parser.add_argument("--systemd-root", type=Path, default=Path("/etc/systemd/system"))
    parser.add_argument("--runtime-artifacts", type=Path)
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
        return _fixture(args) if mode == "fixture" else _production(args)
    except (AdapterError, OSError, json.JSONDecodeError) as exc:
        print(redact_text(str(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
