#!/usr/bin/env python3
"""Validate and atomically install an authenticated ASRSub runtime bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from production_adapter_common import (
    AdapterError,
    atomic_write_json,
    canonical_json,
    default_owner,
    environment_secret_values,
    ensure_directory,
    ensure_existing_directory,
    ensure_no_symlink,
    ensure_parent_directory,
    filesystem_identity,
    read_json,
    redact_text,
    require_absolute,
    require_hex,
    run_argv,
    sha256_file,
)


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
            raise AdapterError(f"invalid manifest mode: {value}") from exc
    else:
        raise AdapterError("manifest member mode is required")
    if mode < 0 or mode & ~0o777:
        raise AdapterError(f"manifest mode is unsafe: {value}")
    return mode


def _load_manifest(bundle_root: Path, manifest_path: Path, release_sha: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    manifest_path = require_absolute(manifest_path, name="bundle manifest")
    ensure_no_symlink(manifest_path, name="bundle manifest", allow_missing=False)
    manifest = read_json(manifest_path, name="bundle manifest")
    if not isinstance(manifest, dict) or manifest.get("schema") not in _ALLOWED_MANIFEST_SCHEMAS:
        raise AdapterError("bundle manifest has an unsupported schema")
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
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise AdapterError(f"invalid expected hash for bundle member: {path}")
        normalized.append({"path": path, "sha256": digest, "mode": _parse_mode_value(item.get("mode"))})
    manifest_hash = hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest, normalized, manifest_hash


def _relative(path: Path, root: Path) -> str | None:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def _validate_bundle_tree(bundle_root: Path, manifest_path: Path, members: list[dict[str, Any]]) -> None:
    allowed = {item["path"] for item in members}
    manifest_rel = _relative(manifest_path, bundle_root)
    if manifest_rel is not None:
        allowed.add(manifest_rel)
    for current, directories, files in os.walk(bundle_root, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *files):
            path = current_path / name
            try:
                st = os.lstat(path)
            except OSError as exc:
                raise AdapterError(f"cannot inspect bundle member: {exc.strerror or exc}") from exc
            if stat.S_ISLNK(st.st_mode):
                raise AdapterError(f"bundle contains a symlink: {path}")
            relative = path.relative_to(bundle_root).as_posix()
            if stat.S_ISREG(st.st_mode) and relative not in allowed:
                raise AdapterError(f"bundle contains an unlisted file: {relative}")
            if not stat.S_ISDIR(st.st_mode) and not stat.S_ISREG(st.st_mode):
                raise AdapterError(f"bundle contains an unsupported entry: {relative}")
    for item in members:
        source = bundle_root / Path(item["path"])
        ensure_no_symlink(source, name="bundle member", allow_missing=False)
        try:
            st = os.lstat(source)
        except OSError as exc:
            raise AdapterError(f"cannot inspect bundle member {item['path']}: {exc.strerror or exc}") from exc
        if not stat.S_ISREG(st.st_mode):
            raise AdapterError(f"bundle member is not a regular file: {item['path']}")
        if stat.S_IMODE(st.st_mode) != item["mode"]:
            raise AdapterError(f"bundle member mode mismatch: {item['path']}")
        if sha256_file(source, name=f"bundle member {item['path']}") != item["sha256"]:
            raise AdapterError(f"bundle member hash mismatch: {item['path']}")


def _approval_release(approval_path: Path, release_sha: str) -> None:
    approval_path = require_absolute(approval_path, name="authenticated approval")
    ensure_no_symlink(approval_path, name="authenticated approval", allow_missing=False)
    try:
        raw = approval_path.read_bytes()
    except OSError as exc:
        raise AdapterError(f"cannot read authenticated approval: {exc.strerror or exc}") from exc
    if not raw:
        raise AdapterError("authenticated approval is empty")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if isinstance(value, dict) and "release_sha" in value and value["release_sha"] != release_sha:
        raise AdapterError("authenticated approval release SHA does not match the caller")


def _copy_verified(source: Path, destination: Path, *, mode: int, uid: int, gid: int) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise AdapterError(f"cannot open bundle member: {exc.strerror or exc}") from exc
    try:
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
    except OSError as exc:
        os.close(source_fd)
        raise AdapterError(f"cannot stage bundle member: {exc.strerror or exc}") from exc
    try:
        os.fchmod(destination_fd, mode)
        os.fchown(destination_fd, uid, gid)
        with os.fdopen(source_fd, "rb", closefd=True) as source_stream, os.fdopen(destination_fd, "wb", closefd=True) as destination_stream:
            shutil.copyfileobj(source_stream, destination_stream, length=1024 * 1024)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
    except OSError as exc:
        raise AdapterError(f"cannot stage bundle member: {exc.strerror or exc}") from exc


def _stage_bundle(bundle_root: Path, members: list[dict[str, Any]], stage: Path, *, uid: int, gid: int) -> None:
    for item in members:
        destination = stage / Path(item["path"])
        ensure_directory(destination.parent, mode=0o755, uid=uid, gid=gid, name="bundle staging directory")
        _copy_verified(bundle_root / Path(item["path"]), destination, mode=item["mode"], uid=uid, gid=gid)
        if sha256_file(destination, name="staged bundle member") != item["sha256"]:
            raise AdapterError(f"staged bundle member hash mismatch: {item['path']}")


def _validate_target_tree(target: Path) -> None:
    for current, directories, files in os.walk(target, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *files):
            path = current_path / name
            if stat.S_ISLNK(os.lstat(path).st_mode):
                raise AdapterError(f"target root contains a symlink: {path}")


def _commit_stage(stage: Path, target: Path, *, target_mode: int, uid: int, gid: int) -> None:
    ensure_no_symlink(target, name="bundle target", allow_missing=True)
    target_exists = os.path.lexists(target)
    if target_exists:
        ensure_existing_directory(target, name="bundle target")
        target_stat = os.lstat(target)
        if stat.S_IMODE(target_stat.st_mode) != target_mode:
            raise AdapterError(
                f"bundle target has mode {stat.S_IMODE(target_stat.st_mode):04o}, expected {target_mode:04o}"
            )
        if target_stat.st_uid != uid or target_stat.st_gid != gid:
            raise AdapterError("bundle target owner does not match the expected owner")
    if not target_exists:
        os.replace(stage, target)
        os.chmod(target, target_mode)
        os.chown(target, uid, gid)
        return
    _validate_target_tree(target)
    try:
        next(target.iterdir())
    except StopIteration:
        os.replace(stage, target)
        os.chmod(target, target_mode)
        os.chown(target, uid, gid)
        return
    for current, directories, files in os.walk(stage, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(stage)
        target_dir = target / relative_dir
        if relative_dir != Path("."):
            ensure_directory(target_dir, mode=0o755, uid=uid, gid=gid, name="bundle target directory")
        for name in files:
            source = current_path / name
            destination = target_dir / name
            ensure_no_symlink(destination, name="bundle target member")
            os.replace(source, destination)
    shutil.rmtree(stage, ignore_errors=True)
    os.chmod(target, target_mode)
    os.chown(target, uid, gid)


def _production(args: argparse.Namespace) -> int:
    required = {
        "bundle root": args.bundle_root,
        "bundle manifest": args.manifest,
        "authenticated approval": args.approval,
        "verification command": args.verify_command,
        "release SHA": args.release_sha,
        "bundle target": args.target_root,
        "output": args.output,
    }
    for label, value in required.items():
        if value is None:
            raise AdapterError(f"production mode requires {label}")
    release_sha = require_hex(args.release_sha, name="release SHA", length=40)
    bundle_root = ensure_existing_directory(require_absolute(args.bundle_root, name="bundle root"), name="bundle root")
    target = require_absolute(args.target_root, name="bundle target")
    manifest_path = require_absolute(args.manifest, name="bundle manifest")
    if target == bundle_root:
        raise AdapterError("bundle target must differ from bundle root")
    uid, gid = default_owner()
    manifest, members, manifest_hash = _load_manifest(bundle_root, manifest_path, release_sha)
    _validate_bundle_tree(bundle_root, manifest_path, members)
    _approval_release(args.approval, release_sha)
    verifier = require_absolute(args.verify_command, name="verification command")
    ensure_no_symlink(verifier, name="verification command", allow_missing=False)
    try:
        verifier_stat = os.lstat(verifier)
    except OSError as exc:
        raise AdapterError(f"cannot inspect verification command: {exc.strerror or exc}") from exc
    if not stat.S_ISREG(verifier_stat.st_mode) or not os.access(verifier, os.X_OK):
        raise AdapterError("verification command must be an executable regular file")
    verification = run_argv(
        [
            verifier,
            "--bundle-root",
            bundle_root,
            "--manifest",
            manifest_path,
            "--approval",
            args.approval,
            "--release-sha",
            release_sha,
        ],
        cwd=bundle_root,
        secret_values=environment_secret_values(),
    )
    target_mode = args.target_mode
    if target_mode & ~0o777:
        raise AdapterError("target mode must contain only permission bits")
    ensure_no_symlink(target, name="bundle target", allow_missing=True)
    target_identity = None
    if not args.dry_run:
        parent = ensure_parent_directory(target, name="bundle target")
        stage_name = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=parent))
        try:
            os.chmod(stage_name, target_mode)
            os.chown(stage_name, uid, gid)
            _stage_bundle(bundle_root, members, stage_name, uid=uid, gid=gid)
            _commit_stage(stage_name, target, target_mode=target_mode, uid=uid, gid=gid)
        except Exception:
            shutil.rmtree(stage_name, ignore_errors=True)
            raise
        target_identity = filesystem_identity(target, name="bundle target")
    receipt = {
        "schema": "runtime-bundle-install-receipt-v1",
        "release_sha": release_sha,
        "manifest_sha256": manifest_hash,
        "members": members,
        "approval": {"path": os.fspath(args.approval), "verified": True},
        "verification": verification,
        "target_root": os.fspath(target),
        "target_identity": target_identity,
        "dry_run": bool(args.dry_run),
    }
    output = require_absolute(args.output, name="install receipt output")
    ensure_parent_directory(output, name="install receipt output")
    atomic_write_json(output, receipt, mode=0o600, uid=uid, gid=gid, name="install receipt")
    return 0


def _mode(args: argparse.Namespace) -> str:
    fixture = bool(args.check_fixture)
    production = bool(args.production or args.mode == "production")
    if args.mode == "fixture" and production:
        raise AdapterError("fixture and production modes are mutually exclusive")
    if production and fixture:
        raise AdapterError("production mode rejects --check-fixture")
    if not production and not fixture:
        raise AdapterError("select --production explicitly or use --check-fixture")
    return "production" if production else "fixture"


def _octal(value: str) -> int:
    try:
        return int(value, 8)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("mode must be an octal value") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "production"))
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--check-fixture", action="store_true")
    parser.add_argument("--bundle-root", "--bundle-dir", dest="bundle_root", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--approval", "--authenticated-handoff", dest="approval", type=Path)
    parser.add_argument("--verify-command", "--verifier", dest="verify_command", type=Path)
    parser.add_argument("--release-sha")
    parser.add_argument("--target-root", "--test-root", dest="target_root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--target-mode", type=_octal, default=0o755)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        mode = _mode(args)
        return _fixture() if mode == "fixture" else _production(args)
    except (AdapterError, OSError) as exc:
        print(redact_text(str(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
