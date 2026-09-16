#!/usr/bin/env python3
"""Build and sign a closed ASRSub runtime bundle.

Fixture mode only emits the disposable approved-image projection.  Production
and test-seam bundle modes require explicit existing release inputs and a real
private-key file descriptor; signatures are produced by fixed /usr/bin/openssl.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


OPENSSL = Path("/usr/bin/openssl")
RELEASE_RE = set("0123456789abcdef")
RUNTIME_MEMBERS = frozenset(
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
        "production_entrypoint.py",
        "production_adapter_common.py",
        "deploy_docker.py",
        "compose.yaml",
    }
)
SYSTEMD_MEMBERS = frozenset(
    {
        "systemd/asrsub-recovery.service",
        "systemd/asrsub-runtime.service",
        "systemd/docker.service.d/asrsub-recovery.conf",
    }
)
EXECUTABLES = frozenset(
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
MEMBER_MODES = {
    **{name: 0o755 for name in EXECUTABLES},
    **{name: 0o644 for name in RUNTIME_MEMBERS - EXECUTABLES},
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def absolute(path: Path, *, name: str) -> Path:
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError(f"{name} must be an absolute path without traversal")
    return path


def release_sha(value: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or value.lower() != value or any(char not in RELEASE_RE for char in value):
        raise ValueError("release SHA must be exactly 40 lowercase hexadecimal characters")
    return value


def _regular(path: Path, *, name: str, mode: int | None = None) -> None:
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"required {name} is absent") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ValueError(f"required {name} is not a regular file")
    if mode is not None and stat.S_IMODE(st.st_mode) != mode:
        raise ValueError(f"{name} mode must be {mode:04o}")


def _tree(root: Path, expected: set[str], *, name: str) -> None:
    actual: set[str] = set()
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            path = current_path / directory
            if path.is_symlink() or not path.is_dir():
                raise ValueError(f"{name} contains an unsafe directory")
        for filename in files:
            path = current_path / filename
            relative = path.relative_to(root).as_posix()
            _regular(path, name=f"{name} member {relative}")
            actual.add(relative)
    if actual != expected:
        raise ValueError(f"{name} inventory mismatch: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")


def _manifest(runtime_source: Path, systemd_source: Path, release: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _tree(runtime_source, set(RUNTIME_MEMBERS), name="runtime release input")
    _tree(systemd_source, {path.removeprefix("systemd/") for path in SYSTEMD_MEMBERS}, name="systemd release input")
    members: list[dict[str, Any]] = []
    for relative in sorted(RUNTIME_MEMBERS):
        source = runtime_source / relative
        _regular(source, name=f"runtime member {relative}", mode=MEMBER_MODES[relative])
        members.append({
            "path": relative,
            "install_root": "runtime",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "mode": f"{MEMBER_MODES[relative]:04o}",
        })
    for path in sorted(SYSTEMD_MEMBERS):
        relative = path.removeprefix("systemd/")
        source = systemd_source / relative
        _regular(source, name=f"systemd member {path}", mode=0o644)
        members.append({
            "path": path,
            "install_root": "systemd",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "mode": "0644",
        })
    compose = runtime_source / "compose.yaml"
    value = {
        "schema": "runtime-bundle-manifest-v1",
        "release_sha": release,
        "members": members,
        "compose_sha256": hashlib.sha256(compose.read_bytes()).hexdigest(),
    }
    return value, members


def _sign_detached(data: Path, signature: Path, key_fd: int) -> None:
    if not isinstance(key_fd, int) or key_fd < 0:
        raise ValueError("a real signing key FD is required")
    try:
        st = os.fstat(key_fd)
    except OSError as exc:
        raise ValueError("signing key FD is not open") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("signing key FD must refer to a regular file")
    signature.parent.mkdir(parents=True, exist_ok=True)
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
        raise ValueError("fixed OpenSSL signing failed") from exc
    if completed.returncode != 0:
        try:
            signature.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError("fixed OpenSSL signing rejected the supplied key")
    os.chmod(signature, 0o600)


def _copy_tree(runtime_source: Path, systemd_source: Path, output: Path, members: list[dict[str, Any]]) -> None:
    for item in members:
        source_root = runtime_source if item["install_root"] == "runtime" else systemd_source
        relative = item["path"] if item["install_root"] == "runtime" else item["path"].removeprefix("systemd/")
        source = source_root / relative
        destination = output / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        os.chmod(destination, int(item["mode"], 8))
        if hashlib.sha256(destination.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"copied bundle member hash mismatch: {item['path']}")


def package_bundle(args: argparse.Namespace, *, test_seam: bool) -> int:
    if args.runtime_source_root is None or args.systemd_source_root is None or args.output_root is None or args.release_sha is None:
        raise ValueError("bundle mode requires --runtime-source-root, --systemd-source-root, --output-root, and --release-sha")
    if args.key_fd is None:
        raise ValueError("bundle mode requires a real signing key through --key-fd")
    runtime_source = absolute(args.runtime_source_root, name="runtime source root")
    systemd_source = absolute(args.systemd_source_root, name="systemd source root")
    output_root = absolute(args.output_root, name="bundle output root")
    release = release_sha(args.release_sha)
    if not runtime_source.is_dir() or not systemd_source.is_dir():
        raise ValueError("required release input directory is absent")
    if output_root.exists() or output_root.is_symlink():
        raise ValueError("bundle output root must not already exist")
    manifest_output = absolute(args.manifest_output or output_root.parent / "bundle-manifest.json", name="manifest output")
    signature_output = absolute(args.signature_output or output_root.parent / "bundle-manifest.sig", name="signature output")
    if manifest_output.exists() or signature_output.exists():
        raise ValueError("manifest and signature outputs must not already exist")
    manifest, members = _manifest(runtime_source, systemd_source, release)
    parent = output_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.stage-", dir=parent))
    manifest_stage = parent / f".{manifest_output.name}.stage-{os.getpid()}"
    signature_stage = parent / f".{signature_output.name}.stage-{os.getpid()}"
    output_created = False
    try:
        _copy_tree(runtime_source, systemd_source, stage, members)
        manifest_stage.write_bytes(canonical(manifest) + b"\n")
        os.chmod(manifest_stage, 0o600)
        _sign_detached(manifest_stage, signature_stage, args.key_fd)
        os.replace(stage, output_root)
        output_created = True
        os.replace(manifest_stage, manifest_output)
        os.replace(signature_stage, signature_output)
    except Exception:
        if output_created:
            shutil.rmtree(output_root, ignore_errors=True)
        else:
            shutil.rmtree(stage, ignore_errors=True)
        for path in (manifest_stage, signature_stage):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return 0


def emit_approved_image(args: argparse.Namespace) -> int:
    if not args.fixture:
        raise ValueError("approved-image projection is fixture-only; use --fixture explicitly")
    if args.image_ref is None or args.release_sha is None or args.output is None:
        raise ValueError("fixture approved-image mode requires --image-ref, --release-sha, and --output")
    image = args.image_ref
    release = release_sha(args.release_sha)
    if not image.startswith("ghcr.io/bedasrv/asrsub@sha256:") or len(image.rsplit(":", 1)[-1]) != 64:
        raise ValueError("invalid approved image digest")
    value = {
        "schema": "approved-image-v1",
        "image_ref": image,
        "image_digest": image.rsplit(":", 1)[-1],
        "release_sha": release,
        "platform": "linux/amd64",
        "created_epoch_ns": 0,
        "fixture_only": True,
    }
    output = absolute(args.output, name="approved image output")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical(value) + b"\n")
    os.chmod(output, 0o600)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "production", "test-seam"))
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--test-seam", action="store_true")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--emit-approved-image", action="store_true")
    parser.add_argument("--approved-image-hash", type=Path)
    parser.add_argument("--image-ref")
    parser.add_argument("--release-sha")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime-source-root", type=Path)
    parser.add_argument("--systemd-source-root", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--manifest-output", type=Path)
    parser.add_argument("--signature-output", type=Path)
    parser.add_argument("--key-fd", type=int)
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
        if args.emit_approved_image or args.approved_image_hash is not None:
            if args.approved_image_hash is not None:
                if not fixture:
                    raise ValueError("approved-image hashing is fixture-only")
                print(hashlib.sha256(b"asrsub-approved-image-v1\\0" + args.approved_image_hash.read_bytes()).hexdigest())
                return 0
            return emit_approved_image(args)
        if fixture:
            raise ValueError("fixture mode only supports --emit-approved-image or --approved-image-hash")
        return package_bundle(args, test_seam=test_seam)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
