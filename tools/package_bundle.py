#!/usr/bin/python3
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
from pathlib import Path
from typing import Any


OPENSSL = Path("/usr/bin/openssl")
GIT = Path("/usr/bin/git")
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
    path = Path(os.fspath(path))
    if any(part == ".." for part in path.parts):
        raise ValueError(f"{name} must not contain path traversal")
    if path.is_absolute():
        return path
    root = Path.cwd()
    try:
        candidate = root / path
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{name} must stay within the current working directory") from exc
    return candidate


def release_sha(value: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or value.lower() != value or any(char not in RELEASE_RE for char in value):
        raise ValueError("release SHA must be exactly 40 lowercase hexadecimal characters")
    return value


def release_sha_from_git(cwd: Path | None = None) -> str:
    workdir = Path.cwd() if cwd is None else cwd
    try:
        completed = subprocess.run(
            [os.fspath(GIT), "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"],
            cwd=workdir,
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            env={
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("cannot resolve a full lowercase 40-character HEAD SHA from git") from exc
    raw = completed.stdout
    if completed.returncode != 0 or raw.count("\n") != 1 or not raw.endswith("\n"):
        raise ValueError("cannot resolve a full lowercase 40-character HEAD SHA from git")
    try:
        return release_sha(raw[:-1])
    except ValueError as exc:
        raise ValueError("git HEAD is not a full lowercase 40-character SHA") from exc


def _git_repository_root(cwd: Path | None = None) -> Path:
    workdir = Path.cwd() if cwd is None else cwd
    try:
        completed = subprocess.run(
            [os.fspath(GIT), "rev-parse", "--show-toplevel"],
            cwd=workdir,
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            env={
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("production signer must run from the repository root") from exc
    raw = completed.stdout
    if completed.returncode != 0 or raw.count("\n") != 1 or not raw.endswith("\n"):
        raise ValueError("production signer must run from the repository root")
    try:
        repository = Path(raw[:-1]).resolve(strict=True)
        current = workdir.resolve(strict=True)
    except OSError as exc:
        raise ValueError("production signer must run from the repository root") from exc
    if current != repository:
        raise ValueError("production signer must run from the repository root")
    return repository


def _regular(path: Path, *, name: str, mode: int | None = None) -> None:
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"required {name} is absent") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ValueError(f"required {name} is not a regular file")
    if mode is not None and stat.S_IMODE(st.st_mode) != mode:
        raise ValueError(f"{name} mode must be {mode:04o}")


O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def _check_directory_fd(fd: int, *, name: str) -> None:
    try:
        st = os.fstat(fd)
    except OSError as exc:
        raise ValueError(f"cannot inspect {name}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"{name} contains a non-directory path component")
    mode = stat.S_IMODE(st.st_mode)
    if (mode & 0o002 and not mode & stat.S_ISVTX) or st.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise ValueError(f"{name} has an unsafe directory mode")


def _open_directory(path: Path, *, name: str) -> int:
    """Open every directory component without following a replacement link."""
    path = absolute(path, name=name)
    try:
        current = os.open(os.path.sep, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    except OSError as exc:
        raise ValueError(f"cannot open {name}") from exc
    try:
        for part in path.parts:
            if part == path.anchor:
                continue
            try:
                child = os.open(part, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=current)
            except OSError as exc:
                raise ValueError(f"cannot open {name} path component") from exc
            try:
                _check_directory_fd(child, name=name)
            except Exception:
                os.close(child)
                raise
            os.close(current)
            current = child
        return current
    except Exception:
        os.close(current)
        raise


def _capture_regular_fd(fd: int, *, name: str, mode: int) -> bytes:
    try:
        st = os.fstat(fd)
    except OSError as exc:
        raise ValueError(f"cannot inspect {name}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"{name} is not a regular file")
    if stat.S_IMODE(st.st_mode) != mode:
        raise ValueError(f"{name} mode must be {mode:04o}")
    data = bytearray()
    try:
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            data.extend(block)
        after = os.fstat(fd)
    except OSError as exc:
        raise ValueError(f"cannot capture {name}") from exc
    if after.st_dev != st.st_dev or after.st_ino != st.st_ino or after.st_size != len(data):
        raise ValueError(f"{name} changed while it was captured")
    return bytes(data)


def _capture_tree(root: Path, expected: set[str], modes: dict[str, int], *, name: str) -> dict[str, bytes]:
    """Capture the entire fixed input tree through directory-relative FDs."""
    root_fd = _open_directory(root, name=name)
    captured: dict[str, bytes] = {}
    directories: set[str] = set()

    def visit(directory_fd: int, prefix: str = "") -> None:
        try:
            entries = list(os.scandir(directory_fd))
        except OSError as exc:
            raise ValueError(f"cannot enumerate {name}") from exc
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            try:
                if entry.is_symlink():
                    raise ValueError(f"{name} contains an unsafe directory or file")
                if entry.is_dir(follow_symlinks=False):
                    child = os.open(entry.name, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=directory_fd)
                    try:
                        _check_directory_fd(child, name=name)
                        directories.add(relative)
                        visit(child, relative)
                    finally:
                        os.close(child)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise ValueError(f"{name} contains an unsupported entry")
                if relative not in expected:
                    raise ValueError(f"{name} inventory mismatch: unexpected={relative}")
                mode = modes[relative]
                file_fd = os.open(entry.name, os.O_RDONLY | O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    captured[relative] = _capture_regular_fd(file_fd, name=f"{name} member {relative}", mode=mode)
                finally:
                    os.close(file_fd)
            except OSError as exc:
                raise ValueError(f"cannot capture {name} member {relative}") from exc

    try:
        visit(root_fd)
    finally:
        os.close(root_fd)
    expected_directories = {
        "/".join(parts[:index])
        for value in expected
        for parts in [value.split("/")]
        for index in range(1, len(parts))
    }
    if set(captured) != expected or directories != expected_directories:
        raise ValueError(
            f"{name} inventory mismatch: missing={sorted(expected - set(captured))}, "
            f"extra={sorted(set(captured) - expected)}, "
            f"unexpected_directories={sorted(directories - expected_directories)}"
        )
    return captured


def _manifest(runtime_source: Path, systemd_source: Path, release: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    captured = {
        "runtime": _capture_tree(runtime_source, set(RUNTIME_MEMBERS), MEMBER_MODES, name="runtime release input"),
        "systemd": _capture_tree(
            systemd_source,
            {path.removeprefix("systemd/") for path in SYSTEMD_MEMBERS},
            {path.removeprefix("systemd/"): 0o644 for path in SYSTEMD_MEMBERS},
            name="systemd release input",
        ),
    }
    return _manifest_from_captured(captured, release, signing_mode="production")


def _manifest_from_captured(
    captured: dict[str, dict[str, bytes]],
    release: str,
    *,
    signing_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    members: list[dict[str, Any]] = []
    for relative in sorted(RUNTIME_MEMBERS):
        data = captured["runtime"][relative]
        members.append({
            "path": relative,
            "install_root": "runtime",
            "sha256": hashlib.sha256(data).hexdigest(),
            "mode": f"{MEMBER_MODES[relative]:04o}",
        })
    for path in sorted(SYSTEMD_MEMBERS):
        relative = path.removeprefix("systemd/")
        data = captured["systemd"][relative]
        members.append({
            "path": path,
            "install_root": "systemd",
            "sha256": hashlib.sha256(data).hexdigest(),
            "mode": "0644",
        })
    value = {
        "schema": "runtime-bundle-manifest-v1",
        "signing_mode": signing_mode,
        "release_sha": release,
        "members": members,
        "compose_sha256": hashlib.sha256(captured["runtime"]["compose.yaml"]).hexdigest(),
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
    _fsync_file(signature, name="bundle signature stage")


def _fsync_file(path: Path, *, name: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"{name} is not a regular file")
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ValueError(f"cannot fsync {name}") from exc


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ValueError("cannot fsync publication directory") from exc


def _write_private_file(path: Path, data: bytes, *, mode: int, name: str, create: bool = True) -> None:
    fd = -1
    try:
        flags = os.O_WRONLY | O_NOFOLLOW
        flags |= os.O_CREAT | os.O_EXCL if create else os.O_TRUNC
        fd = os.open(path, flags, mode)
        os.fchmod(fd, mode)
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
        os.fsync(fd)
    except OSError as exc:
        raise ValueError(f"cannot write {name}") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _new_private_stage_file(parent: Path, *, prefix: str) -> Path:
    try:
        fd, raw = tempfile.mkstemp(prefix=prefix, dir=parent)
        os.fchmod(fd, 0o600)
        os.close(fd)
    except OSError as exc:
        raise ValueError("cannot create private signer stage") from exc
    return Path(raw)


def _copy_tree(captured: dict[str, dict[str, bytes]], output: Path, members: list[dict[str, Any]]) -> None:
    for item in members:
        relative = item["path"] if item["install_root"] == "runtime" else item["path"].removeprefix("systemd/")
        data = captured[item["install_root"]][relative]
        destination = output / item["path"]
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_private_file(destination, data, mode=int(item["mode"], 8), name=f"bundle member {item['path']}")
        _fsync_directory(destination.parent)
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError(f"copied bundle member hash mismatch: {item['path']}")


def _remove_private_path(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        return
    try:
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _validate_production_layout(args: argparse.Namespace) -> tuple[Path, Path, Path, Path, Path]:
    _git_repository_root()
    expected = {
        "runtime source root": Path("release/runtime"),
        "systemd source root": Path("release/systemd"),
        "bundle output root": Path("release/asrsub-runtime-bundle"),
        "manifest output": Path("release/bundle-manifest.json"),
        "signature output": Path("release/bundle-manifest.sig"),
    }
    values = {
        "runtime source root": args.runtime_source_root,
        "systemd source root": args.systemd_source_root,
        "bundle output root": args.output_root,
        "manifest output": args.manifest_output or expected["manifest output"],
        "signature output": args.signature_output or expected["signature output"],
    }
    for name, value in values.items():
        if value is None or Path(value) != expected[name] or Path(value).is_absolute():
            raise ValueError(f"production {name} must use the fixed relative release path")
    return tuple(absolute(values[name], name=name) for name in expected)  # type: ignore[return-value]


def _ensure_empty_publication_path(path: Path, *, name: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_fd = _open_directory(path.parent, name=f"{name} parent")
    os.close(parent_fd)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(f"cannot inspect {name}") from exc
    raise ValueError(f"{name} must not already exist")


def package_bundle(args: argparse.Namespace, *, test_seam: bool) -> int:
    if args.runtime_source_root is None or args.systemd_source_root is None or args.output_root is None:
        raise ValueError("bundle mode requires --runtime-source-root, --systemd-source-root, and --output-root")
    if (args.release_sha is None) == (not args.release_sha_from_git):
        raise ValueError("bundle mode requires exactly one of --release-sha or --release-sha-from-git")
    if args.key_fd is None:
        raise ValueError("bundle mode requires a real signing key through --key-fd")
    if test_seam:
        runtime_source = absolute(args.runtime_source_root, name="runtime source root")
        systemd_source = absolute(args.systemd_source_root, name="systemd source root")
        output_root = absolute(args.output_root, name="bundle output root")
        manifest_output = absolute(args.manifest_output or output_root.parent / "bundle-manifest.json", name="manifest output")
        signature_output = absolute(args.signature_output or output_root.parent / "bundle-manifest.sig", name="signature output")
    else:
        if args.release_sha_from_git and "RELEASE_SHA" in os.environ:
            raise ValueError("ambient RELEASE_SHA is not accepted in production bundle mode")
        runtime_source, systemd_source, output_root, manifest_output, signature_output = _validate_production_layout(args)
    if not runtime_source.is_dir() or not systemd_source.is_dir():
        raise ValueError("required release input directory is absent")
    _ensure_empty_publication_path(output_root, name="bundle output root")
    _ensure_empty_publication_path(manifest_output, name="manifest output")
    _ensure_empty_publication_path(signature_output, name="signature output")
    if len({output_root, manifest_output, signature_output}) != 3:
        raise ValueError("bundle publication paths must be distinct")
    release = release_sha_from_git() if args.release_sha_from_git else release_sha(args.release_sha)
    captured = {
        "runtime": _capture_tree(runtime_source, set(RUNTIME_MEMBERS), MEMBER_MODES, name="runtime release input"),
        "systemd": _capture_tree(
            systemd_source,
            {path.removeprefix("systemd/") for path in SYSTEMD_MEMBERS},
            {path.removeprefix("systemd/"): 0o644 for path in SYSTEMD_MEMBERS},
            name="systemd release input",
        ),
    }
    manifest, members = _manifest_from_captured(captured, release, signing_mode="test-seam" if test_seam else "production")
    parent = output_root.parent
    manifest_parent = manifest_output.parent
    signature_parent = signature_output.parent
    for publication_parent in {parent, manifest_parent, signature_parent}:
        publication_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_fd = _open_directory(publication_parent, name="bundle publication parent")
        os.close(parent_fd)
    stage: Path | None = None
    manifest_stage: Path | None = None
    signature_stage: Path | None = None
    try:
        stage = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.stage-", dir=parent))
        os.chmod(stage, 0o700)
        manifest_stage = _new_private_stage_file(manifest_parent, prefix=f".{manifest_output.name}.stage-")
        signature_stage = _new_private_stage_file(signature_parent, prefix=f".{signature_output.name}.stage-")
        _copy_tree(captured, stage, members)
        _write_private_file(manifest_stage, canonical(manifest) + b"\n", mode=0o600, name="bundle manifest stage", create=False)
        _sign_detached(manifest_stage, signature_stage, args.key_fd)
        _fsync_directory(stage)
        os.replace(stage, output_root)
        _fsync_directory(parent)
        os.replace(manifest_stage, manifest_output)
        _fsync_directory(manifest_parent)
        os.replace(signature_stage, signature_output)
        _fsync_file(manifest_output, name="bundle manifest")
        _fsync_file(signature_output, name="bundle signature")
        _fsync_directory(signature_parent)
        for publication_parent in {parent, manifest_parent, signature_parent}:
            _fsync_directory(publication_parent)
    except Exception:
        for path in (output_root, manifest_output, signature_output, stage, manifest_stage, signature_stage):
            if path is not None:
                _remove_private_path(path)
        try:
            for publication_parent in {parent, manifest_parent, signature_parent}:
                _fsync_directory(publication_parent)
        except ValueError:
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
    parser.add_argument("--release-sha-from-git", action="store_true")
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
