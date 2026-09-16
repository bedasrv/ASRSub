#!/usr/bin/env python3
"""Provision ASRSub StateFs with explicit fixture and production modes."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from production_adapter_common import (
    AdapterError,
    atomic_write_json,
    create_empty_file,
    default_owner,
    ensure_directory,
    filesystem_identity,
    now_ns,
    read_json,
    redact_text,
    require_absolute,
    require_hex,
)


def _fixture(args: argparse.Namespace) -> int:
    if args.fixture_root is None:
        raise AdapterError("fixture mode requires --fixture-root")
    root = args.fixture_root
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    (root / "discord-notifications" / "quarantine").mkdir(parents=True, exist_ok=True)
    (root / "deployment-admission").mkdir(parents=True, exist_ok=True)
    for path in (
        root / "state.jsonl",
        root / "state.jsonl.lock",
        root / "discord-notifications" / "state.json.lock",
        root / "deployment-admission" / "admission.lock",
    ):
        path.touch(exist_ok=True)
        os.chmod(path, 0o600)
    admission = root / "deployment-admission" / "admission.json"
    admission.write_text(
        '{"schema":"admission-v1","mode":"recovery_required","generation":0,"active":[],"updated_epoch_ns":0}\n',
        encoding="utf-8",
    )
    os.chmod(admission, 0o600)
    out = args.evidence_root / "statefs-provision"
    out.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema": "statefs-provision-receipt-v1",
        "implementation_commit": "0" * 40,
        "root_identity": {"device": 0, "inode": 0, "mount_id": 0, "filesystem": "ext4"},
        "entries": [],
        "created_epoch_ns": 0,
    }
    (out / "statefs-provision-receipt.json").write_text(
        json.dumps(receipt, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    return 0


def _owner(args: argparse.Namespace) -> tuple[int, int]:
    current_uid, current_gid = default_owner()
    uid = current_uid if args.owner_uid is None else args.owner_uid
    gid = current_gid if args.owner_gid is None else args.owner_gid
    if uid < 0 or gid < 0:
        raise AdapterError("owner uid/gid must be non-negative")
    return uid, gid


def _production(args: argparse.Namespace) -> int:
    if args.state_root is None:
        raise AdapterError("production mode requires --state-root")
    if args.implementation_commit is None:
        raise AdapterError("production mode requires --implementation-commit")
    commit = require_hex(args.implementation_commit, name="implementation commit", length=40)
    state_root = require_absolute(args.state_root, name="state root")
    evidence_root = require_absolute(args.evidence_root, name="evidence root")
    if state_root == evidence_root:
        raise AdapterError("state root and evidence root must be distinct")
    uid, gid = _owner(args)
    directory_mode = args.directory_mode
    file_mode = args.file_mode
    if directory_mode & ~0o777 or file_mode & ~0o777:
        raise AdapterError("modes must contain only permission bits")

    ensure_directory(state_root, mode=directory_mode, uid=uid, gid=gid, name="state root")
    ensure_directory(evidence_root, mode=directory_mode, uid=uid, gid=gid, name="evidence root")
    root_identity = filesystem_identity(state_root, name="state root")
    evidence_identity = filesystem_identity(evidence_root, name="evidence root")

    directories = (
        state_root / "discord-notifications",
        state_root / "discord-notifications" / "quarantine",
        state_root / "deployment-admission",
    )
    for directory in directories:
        ensure_directory(directory, mode=directory_mode, uid=uid, gid=gid, name="StateFs directory")

    files = (
        state_root / "state.jsonl",
        state_root / "state.jsonl.lock",
        state_root / "discord-notifications" / "state.json.lock",
        state_root / "deployment-admission" / "admission.lock",
    )
    for path in files:
        create_empty_file(path, mode=file_mode, uid=uid, gid=gid, name="StateFs file")

    admission = state_root / "deployment-admission" / "admission.json"
    atomic_write_json(
        admission,
        {
            "schema": "admission-v1",
            "mode": "recovery_required",
            "generation": 0,
            "active": [],
            "updated_epoch_ns": now_ns(),
        },
        mode=file_mode,
        uid=uid,
        gid=gid,
        name="admission file",
    )

    entries: list[dict[str, Any]] = []
    for path in (*directories, *files, admission):
        identity = filesystem_identity(path, name="StateFs entry")
        entries.append(
            {
                "path": os.fspath(path.relative_to(state_root)),
                "kind": "directory" if stat.S_ISDIR(os.lstat(path).st_mode) else "file",
                "identity": identity,
            }
        )
    if filesystem_identity(state_root, name="state root") != root_identity:
        raise AdapterError("state root identity changed during provisioning")
    receipt = {
        "schema": "statefs-provision-receipt-v1",
        "implementation_commit": commit,
        "root_identity": root_identity,
        "evidence_identity": evidence_identity,
        "entries": entries,
        "created_epoch_ns": now_ns(),
    }
    receipt_dir = evidence_root / "statefs-provision"
    ensure_directory(receipt_dir, mode=directory_mode, uid=uid, gid=gid, name="StateFs receipt directory")
    atomic_write_json(
        receipt_dir / "statefs-provision-receipt.json",
        receipt,
        mode=file_mode,
        uid=uid,
        gid=gid,
        name="StateFs receipt",
    )
    return 0


def _mode(args: argparse.Namespace) -> str:
    fixture = args.fixture_root is not None
    production = bool(args.production or args.mode == "production")
    if args.mode == "fixture" and production:
        raise AdapterError("fixture and production modes are mutually exclusive")
    if production and fixture:
        raise AdapterError("production mode rejects --fixture-root")
    if not production and not fixture:
        raise AdapterError("select --production explicitly or provide --fixture-root")
    if args.mode == "fixture" and not fixture:
        raise AdapterError("fixture mode requires --fixture-root")
    return "production" if production else "fixture"


def _octal(value: str) -> int:
    try:
        return int(value, 8)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("mode must be an octal value such as 0700") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "production"))
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--state-root", "--target-root", "--root", dest="state_root", type=Path)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--implementation-commit", "--commit", dest="implementation_commit")
    parser.add_argument("--owner-uid", "--expected-uid", dest="owner_uid", type=int)
    parser.add_argument("--owner-gid", "--expected-gid", dest="owner_gid", type=int)
    parser.add_argument("--directory-mode", type=_octal, default=0o700)
    parser.add_argument("--file-mode", type=_octal, default=0o600)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        mode = _mode(args)
        return _fixture(args) if mode == "fixture" else _production(args)
    except (AdapterError, OSError) as exc:
        print(redact_text(str(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
