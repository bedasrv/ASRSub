#!/usr/bin/env python3
"""Create a detached approval signature with an explicit signer mode.

``--fixture`` is intentionally non-production and writes a marker signature.
``--production`` and ``--test-seam`` require a real private-key FD and sign
canonical approval bytes with fixed /usr/bin/openssl.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any


OPENSSL = Path("/usr/bin/openssl")
ALLOWED_APPROVAL_KEYS = {
    "schema",
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


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def absolute(path: Path, *, name: str) -> Path:
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError(f"{name} must be an absolute path without traversal")
    return path


def _read_canonical(path: Path) -> tuple[dict[str, Any], bytes]:
    path = absolute(path, name="canonical approval")
    try:
        source_stat = os.lstat(path)
    except OSError as exc:
        raise ValueError("canonical approval is absent") from exc
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        raise ValueError("canonical approval must be a regular file")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("canonical approval is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("canonical approval must be a JSON object")
    expected = canonical(value) + b"\n"
    if raw != expected:
        raise ValueError("canonical approval bytes are not canonical")
    return value, raw


def _validate_approval(value: dict[str, Any]) -> None:
    if set(value) != ALLOWED_APPROVAL_KEYS:
        raise ValueError("approval contains missing or unapproved fields")
    if value.get("schema") != "approval-v1":
        raise ValueError("approval schema must be approval-v1")
    release = value.get("release_sha")
    if not isinstance(release, str) or len(release) != 40 or release.lower() != release or any(char not in "0123456789abcdef" for char in release):
        raise ValueError("approval release_sha is invalid")
    for name in ("bundle_sha256", "compose_sha256", "compose_template_sha256", "image_digest"):
        digest = value.get(name)
        if not isinstance(digest, str) or len(digest) != 64 or digest.lower() != digest or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError(f"approval {name} is not a lowercase sha256 identity")
    if not isinstance(value.get("generation"), int) or isinstance(value.get("generation"), bool) or value["generation"] <= 0:
        raise ValueError("approval generation must be positive")
    if value.get("approved_docker_socket") != "default" or value.get("approved_state_root") != "/var/lib/asrsub/state":
        raise ValueError("approval host bindings are not the fixed production values")
    if not isinstance(value.get("notifications_enabled"), bool):
        raise ValueError("approval notifications_enabled must be boolean")
    if not isinstance(value.get("created_epoch_ns"), int) or isinstance(value.get("created_epoch_ns"), bool) or value["created_epoch_ns"] < 0:
        raise ValueError("approval created_epoch_ns is invalid")


def _sign(data: Path, signature: Path, key_fd: int) -> None:
    try:
        st = os.fstat(key_fd)
    except OSError as exc:
        raise ValueError("approval signing key FD is not open") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("approval signing key FD must refer to a regular file")
    try:
        completed = subprocess.run(
            [os.fspath(OPENSSL), "dgst", "-sha256", "-sign", f"/proc/self/fd/{key_fd}", "-out", os.fspath(signature), os.fspath(data)],
            capture_output=True,
            check=False,
            shell=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            pass_fds=(key_fd,),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("fixed OpenSSL approval signing failed") from exc
    if completed.returncode != 0:
        signature.unlink(missing_ok=True)
        raise ValueError("fixed OpenSSL rejected the supplied approval key")
    os.chmod(signature, 0o600)


def sign_approval(args: argparse.Namespace) -> int:
    required = (args.canonical_approval_bytes, args.approval_manifest, args.approval_signature, args.approval_key_fd)
    if any(value is None for value in required):
        raise ValueError("signing mode requires canonical bytes, manifest output, signature output, and --approval-key-fd")
    source = absolute(args.canonical_approval_bytes, name="canonical approval")
    manifest = absolute(args.approval_manifest, name="approval manifest")
    signature = absolute(args.approval_signature, name="approval signature")
    value, raw = _read_canonical(source)
    _validate_approval(value)
    if manifest.exists() or signature.exists():
        raise ValueError("approval outputs must not already exist")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    signature.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_bytes(raw)
    os.chmod(manifest, 0o600)
    try:
        _sign(manifest, signature, args.approval_key_fd)
    except Exception:
        manifest.unlink(missing_ok=True)
        signature.unlink(missing_ok=True)
        raise
    return 0


def fixture_approval(args: argparse.Namespace) -> int:
    if args.canonical_approval_bytes is None or args.approval_manifest is None or args.approval_signature is None:
        raise ValueError("fixture mode requires canonical bytes, approval manifest, and approval signature")
    source = absolute(args.canonical_approval_bytes, name="canonical approval")
    manifest = absolute(args.approval_manifest, name="approval manifest")
    signature = absolute(args.approval_signature, name="approval signature")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    signature.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, manifest)
    signature.write_bytes(b"fixture-approval-signature\n")
    os.chmod(signature, 0o600)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "production", "test-seam"))
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--test-seam", action="store_true")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--canonical-approval-bytes", type=Path)
    parser.add_argument("--approval-manifest", type=Path)
    parser.add_argument("--approval-signature", type=Path)
    parser.add_argument("--approval-key-fd", "--key-fd", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    production = bool(args.production or args.mode == "production")
    test_seam = bool(args.test_seam or args.mode == "test-seam")
    fixture = bool(args.fixture or args.mode == "fixture")
    try:
        if sum((production, test_seam, fixture)) != 1:
            raise ValueError("select exactly one of --production, --test-seam, or --fixture")
        if fixture:
            if args.approval_key_fd is not None:
                raise ValueError("fixture mode rejects signing key FDs")
            return fixture_approval(args)
        return sign_approval(args)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
