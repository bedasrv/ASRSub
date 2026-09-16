#!/usr/bin/env python3
"""Provision ASRSub StateFs with explicit fixture, test-seam, and production modes."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

from production_adapter_common import (
    AdapterError,
    atomic_write_json,
    create_empty_file,
    default_owner,
    ensure_directory,
    ensure_no_symlink,
    ensure_parent_directory,
    environment_secret_values,
    filesystem_identity,
    now_ns,
    read_json,
    redact_text,
    require_absolute,
    require_hex,
)


PRODUCTION_STATE_ROOT = Path("/var/lib/asrsub/state")
PRODUCTION_EVIDENCE_ROOT = Path("/var/lib/asrsub/deploy-state/evidence")
PRODUCTION_UID = 1000
PRODUCTION_GID = 1000
PRODUCTION_DIRECTORY_MODE = 0o700
PRODUCTION_FILE_MODE = 0o600
ALLOWED_FILESYSTEMS = frozenset({"ext4", "xfs", "btrfs", "zfs"})

_STATE_DIRECTORIES = (
    "discord-notifications",
    "discord-notifications/quarantine",
    "deployment-admission",
)
_STATE_FILES = (
    "state.jsonl",
    "state.jsonl.lock",
    "discord-notifications/state.json.lock",
    "deployment-admission/admission.lock",
    "deployment-admission/admission.json",
)


def _fixture(args: argparse.Namespace) -> int:
    if args.fixture_root is None:
        raise AdapterError("fixture mode requires --fixture-root")
    root = require_absolute(args.fixture_root, name="fixture root")
    evidence_root = require_absolute(args.evidence_root, name="fixture evidence root")
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
    out = evidence_root / "statefs-provision"
    out.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema": "statefs-provision-receipt-v1",
        "implementation_commit": "0" * 40,
        "root_identity": {"device": 0, "inode": 0, "mount_id": 0, "filesystem": "fixture"},
        "entries": [],
        "lock_proof": {"held": False, "fixture": True},
        "created_epoch_ns": 0,
        "evidence_eligible": False,
    }
    (out / "statefs-provision-receipt.json").write_text(
        json.dumps(receipt, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    return 0


def _owner(args: argparse.Namespace, *, test_seam: bool) -> tuple[int, int]:
    if not test_seam:
        if args.owner_uid is not None and args.owner_uid != PRODUCTION_UID:
            raise AdapterError("production StateFs owner UID is fixed at 1000")
        if args.owner_gid is not None and args.owner_gid != PRODUCTION_GID:
            raise AdapterError("production StateFs owner GID is fixed at 1000")
        return PRODUCTION_UID, PRODUCTION_GID
    current_uid, current_gid = default_owner()
    uid = current_uid if args.owner_uid is None else args.owner_uid
    gid = current_gid if args.owner_gid is None else args.owner_gid
    if uid < 0 or gid < 0:
        raise AdapterError("owner uid/gid must be non-negative")
    return uid, gid


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise AdapterError("StateFs directory durability check failed") from exc


def _held_lock(path: Path, *, uid: int, gid: int, mode: int) -> Iterator[dict[str, Any]]:
    create_empty_file(path, mode=mode, uid=uid, gid=gid, name="StateFs provisioning lock")
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise AdapterError("cannot open StateFs provisioning lock") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise AdapterError("StateFs provisioning lock is not available") from exc
        yield {"held": True, "path": os.fspath(path), "fd_open": True}
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


@contextlib.contextmanager
def held_lock(path: Path, *, uid: int, gid: int, mode: int) -> Iterator[dict[str, Any]]:
    yield from _held_lock(path, uid=uid, gid=gid, mode=mode)


def _validate_admission(path: Path) -> dict[str, Any]:
    value = read_json(path, name="existing admission")
    if not isinstance(value, dict) or value.get("schema") != "admission-v1":
        raise AdapterError("existing admission is malformed")
    if value.get("mode") not in {"running", "quiescing", "recovery_required"}:
        raise AdapterError("existing admission mode is invalid")
    generation = value.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise AdapterError("existing admission generation is invalid")
    if not isinstance(value.get("active"), list):
        raise AdapterError("existing admission active set is invalid")
    if not isinstance(value.get("updated_epoch_ns"), int) or value["updated_epoch_ns"] < 0:
        raise AdapterError("existing admission timestamp is invalid")
    return value


def _validate_tree(root: Path, *, uid: int, gid: int, directory_mode: int, file_mode: int) -> list[dict[str, Any]]:
    expected_dirs = set(_STATE_DIRECTORIES)
    expected_files = set(_STATE_FILES)
    actual_dirs: set[str] = set()
    actual_files: set[str] = set()
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise AdapterError("StateFs contains an unsafe directory")
            actual_dirs.add(path.relative_to(root).as_posix())
        for name in files:
            path = current_path / name
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise AdapterError("StateFs contains an unsafe file")
            actual_files.add(path.relative_to(root).as_posix())
    if actual_dirs != expected_dirs or actual_files != expected_files:
        raise AdapterError("existing StateFs inventory is ambiguous or incomplete")
    entries: list[dict[str, Any]] = []
    for relative in sorted(expected_dirs | expected_files):
        path = root / relative
        st = os.lstat(path)
        expected_mode = directory_mode if relative in expected_dirs else file_mode
        if stat.S_IMODE(st.st_mode) != expected_mode or st.st_uid != uid or st.st_gid != gid:
            raise AdapterError(f"StateFs metadata mismatch: {relative}")
        identity = filesystem_identity(path, name="StateFs entry")
        entries.append(
            {
                "path": relative,
                "kind": "directory" if relative in expected_dirs else "file",
                "mode": expected_mode,
                "uid": st.st_uid,
                "gid": st.st_gid,
                "identity": identity,
            }
        )
    return entries


def _production(args: argparse.Namespace, *, test_seam: bool) -> int:
    if args.state_root is None:
        raise AdapterError("production mode requires --state-root")
    if args.implementation_commit is None:
        raise AdapterError("production mode requires --implementation-commit")
    if args.evidence_root is None:
        raise AdapterError("production mode requires --evidence-root")
    commit = require_hex(args.implementation_commit, name="implementation commit", length=40)
    state_root = require_absolute(args.state_root, name="state root")
    evidence_root = require_absolute(args.evidence_root, name="evidence root")
    if not test_seam:
        if state_root != PRODUCTION_STATE_ROOT:
            raise AdapterError("production state root must be /var/lib/asrsub/state")
        if evidence_root != PRODUCTION_EVIDENCE_ROOT:
            raise AdapterError("production evidence root is not approved")
    if state_root == evidence_root:
        raise AdapterError("state root and evidence root must be distinct")
    uid, gid = _owner(args, test_seam=test_seam)
    directory_mode = args.directory_mode
    file_mode = args.file_mode
    if test_seam:
        if directory_mode & ~0o777 or file_mode & ~0o777:
            raise AdapterError("modes must contain only permission bits")
    elif directory_mode != PRODUCTION_DIRECTORY_MODE or file_mode != PRODUCTION_FILE_MODE:
        raise AdapterError("production StateFs modes are fixed at 0700/0600")

    root_existed = state_root.exists()
    ensure_directory(state_root, mode=directory_mode, uid=uid, gid=gid, name="state root")
    ensure_directory(evidence_root, mode=directory_mode, uid=uid, gid=gid, name="evidence root")
    root_identity = filesystem_identity(state_root, name="state root")
    evidence_identity = filesystem_identity(evidence_root, name="evidence root")
    if not test_seam and root_identity["filesystem"] not in ALLOWED_FILESYSTEMS:
        raise AdapterError("state root filesystem is not an approved filesystem")
    if not test_seam and evidence_identity["filesystem"] not in ALLOWED_FILESYSTEMS:
        raise AdapterError("evidence root filesystem is not an approved filesystem")

    had_entries = any(state_root.iterdir())
    if root_existed and had_entries:
        existing_names = {entry.name for entry in state_root.iterdir()}
        if not {"discord-notifications", "deployment-admission", "state.jsonl", "state.jsonl.lock"}.issubset(existing_names):
            raise AdapterError("nonempty existing StateFs cannot be repaired")

    lock_path = state_root / "state.jsonl.lock"
    with held_lock(lock_path, uid=uid, gid=gid, mode=file_mode) as lock_proof:
        directories = tuple(state_root / relative for relative in _STATE_DIRECTORIES)
        files = tuple(state_root / relative for relative in _STATE_FILES if relative != "deployment-admission/admission.json")
        if not (root_existed and had_entries):
            for directory in directories:
                ensure_directory(directory, mode=directory_mode, uid=uid, gid=gid, name="StateFs directory")
            for path in files:
                create_empty_file(path, mode=file_mode, uid=uid, gid=gid, name="StateFs file")
            admission = state_root / "deployment-admission" / "admission.json"
            if not admission.exists():
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
        admission = state_root / "deployment-admission" / "admission.json"
        _validate_admission(admission)  # read and preserve; never reset an existing file
        entries = _validate_tree(root=state_root, uid=uid, gid=gid, directory_mode=directory_mode, file_mode=file_mode)
        if filesystem_identity(state_root, name="state root") != root_identity:
            raise AdapterError("state root identity changed during provisioning")
        _fsync_directory(state_root / "discord-notifications")
        _fsync_directory(state_root / "deployment-admission")
        _fsync_directory(state_root)

    receipt = {
        "schema": "statefs-provision-receipt-v1",
        "implementation_commit": commit,
        "root_identity": root_identity,
        "evidence_identity": evidence_identity,
        "entries": entries,
        "lock_proof": lock_proof,
        "created_epoch_ns": now_ns(),
        "evidence_eligible": not test_seam,
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
    seam = bool(args.test_seam or args.mode == "test-seam")
    if args.mode == "fixture" and (production or seam):
        raise AdapterError("fixture, production, and test-seam modes are mutually exclusive")
    if production and (fixture or seam):
        raise AdapterError("production, fixture, and test-seam modes are mutually exclusive")
    if seam and fixture:
        raise AdapterError("test-seam mode rejects --fixture-root")
    if not production and not seam and not fixture:
        raise AdapterError("select --production, --test-seam, or provide --fixture-root")
    return "production" if production else "test-seam" if seam else "fixture"


def _octal(value: str) -> int:
    try:
        return int(value, 8)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("mode must be an octal value such as 0700") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "production", "test-seam"))
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--test-seam", action="store_true")
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
        if mode == "fixture":
            return _fixture(args)
        return _production(args, test_seam=mode == "test-seam")
    except (AdapterError, OSError, UnicodeDecodeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(redact_text(str(exc), secret_values=environment_secret_values()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
