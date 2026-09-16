#!/usr/bin/python3
"""Create a detached approval signature with an explicit signer mode.

``--fixture`` is intentionally non-production and writes a marker signature.
``--production`` and ``--test-seam`` require a real private-key FD and sign
canonical approval bytes with fixed /usr/bin/openssl.  Production approval
creation is bound to the exact lowercase HEAD SHA from fixed /usr/bin/git.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any


OPENSSL = Path("/usr/bin/openssl")
GIT = Path("/usr/bin/git")
RELEASE_RE = set("0123456789abcdef")
ALLOWED_APPROVAL_KEYS = {
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
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


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


def _capture_bytes(path: Path, *, name: str) -> bytes:
    path = absolute(path, name=name)
    parent_fd = _open_directory(path.parent, name=f"{name} parent")
    file_fd = -1
    try:
        try:
            file_fd = os.open(path.name, os.O_RDONLY | O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError(f"required {name} is absent") from exc
        try:
            st = os.fstat(file_fd)
        except OSError as exc:
            raise ValueError(f"cannot inspect {name}") from exc
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{name} must be a regular file")
        if stat.S_IMODE(st.st_mode) & 0o002:
            raise ValueError(f"{name} has an unsafe mode")
        data = bytearray()
        try:
            while True:
                block = os.read(file_fd, 1024 * 1024)
                if not block:
                    break
                data.extend(block)
            after = os.fstat(file_fd)
        except OSError as exc:
            raise ValueError(f"cannot capture {name}") from exc
        if after.st_dev != st.st_dev or after.st_ino != st.st_ino or after.st_size != len(data):
            raise ValueError(f"{name} changed while it was captured")
        return bytes(data)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(parent_fd)


def _read_canonical(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = _capture_bytes(path, name="canonical approval")
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


def release_sha(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 40 or value.lower() != value or any(char not in RELEASE_RE for char in value):
        raise ValueError("approval release_sha is invalid")
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
    return release_sha(raw[:-1])


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


def _validate_approval(value: dict[str, Any], *, signing_mode: str) -> None:
    if set(value) != ALLOWED_APPROVAL_KEYS:
        raise ValueError("approval contains missing or unapproved fields")
    if value.get("schema") != "approval-v1":
        raise ValueError("approval schema must be approval-v1")
    if value.get("signing_mode") != signing_mode:
        raise ValueError(f"approval signing_mode must be {signing_mode}")
    release_sha(value.get("release_sha"))
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
        raise ValueError("cannot fsync approval output directory") from exc


def _ensure_empty_output(path: Path, *, name: str) -> None:
    path = absolute(path, name=name)
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


def _new_private_stage(parent: Path, *, prefix: str) -> Path:
    try:
        fd, raw = tempfile.mkstemp(prefix=prefix, dir=parent)
        os.fchmod(fd, 0o600)
        os.close(fd)
    except OSError as exc:
        raise ValueError("cannot create private approval stage") from exc
    return Path(raw)


def _write_stage(path: Path, data: bytes, *, name: str) -> None:
    fd = -1
    try:
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC | O_NOFOLLOW)
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
        os.fsync(fd)
    except OSError as exc:
        raise ValueError(f"cannot write {name}") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _remove_private_path(path: Path) -> None:
    try:
        st = os.lstat(path)
    except (FileNotFoundError, OSError):
        return
    try:
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            for child in path.iterdir():
                _remove_private_path(child)
            path.rmdir()
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _sign(data: Path, signature: Path, key_fd: int) -> None:
    if not isinstance(key_fd, int) or key_fd < 0:
        raise ValueError("a real signing key FD is required")
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
        raise ValueError("fixed OpenSSL rejected the supplied approval key")
    os.chmod(signature, 0o600)
    _fsync_file(signature, name="approval signature stage")


def _publish(manifest: Path, signature: Path, raw: bytes, *, signature_bytes: bytes | None = None, key_fd: int | None = None) -> None:
    _ensure_empty_output(manifest, name="approval manifest")
    _ensure_empty_output(signature, name="approval signature")
    if manifest == signature:
        raise ValueError("approval output paths must be distinct")
    manifest_parent = manifest.parent
    signature_parent = signature.parent
    manifest_stage: Path | None = None
    signature_stage: Path | None = None
    try:
        manifest_stage = _new_private_stage(manifest_parent, prefix=f".{manifest.name}.stage-")
        signature_stage = _new_private_stage(signature_parent, prefix=f".{signature.name}.stage-")
        _write_stage(manifest_stage, raw, name="approval manifest stage")
        if signature_bytes is not None:
            _write_stage(signature_stage, signature_bytes, name="approval signature stage")
        else:
            if key_fd is None:
                raise ValueError("approval signing requires a real signing key FD")
            _sign(manifest_stage, signature_stage, key_fd)
        os.replace(manifest_stage, manifest)
        _fsync_directory(manifest_parent)
        os.replace(signature_stage, signature)
        _fsync_file(manifest, name="approval manifest")
        _fsync_file(signature, name="approval signature")
        _fsync_directory(signature_parent)
        if signature_parent != manifest_parent:
            _fsync_directory(manifest_parent)
    except Exception:
        for path in (manifest, signature, manifest_stage, signature_stage):
            if path is not None:
                _remove_private_path(path)
        for parent in {manifest_parent, signature_parent}:
            try:
                _fsync_directory(parent)
            except ValueError:
                pass
        raise


def _validate_production_layout(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    _git_repository_root()
    expected = {
        "canonical approval": Path("release/approval-canonical.json"),
        "approval manifest": Path("release/approval.json"),
        "approval signature": Path("release/approval.sig"),
    }
    values = {
        "canonical approval": args.canonical_approval_bytes,
        "approval manifest": args.approval_manifest,
        "approval signature": args.approval_signature,
    }
    for name, value in values.items():
        if value is None or Path(value) != expected[name] or Path(value).is_absolute():
            raise ValueError(f"production {name} must use the fixed relative release path")
    return tuple(absolute(values[name], name=name) for name in expected)  # type: ignore[return-value]


def sign_approval(args: argparse.Namespace, *, test_seam: bool) -> int:
    required = (args.canonical_approval_bytes, args.approval_manifest, args.approval_signature, args.approval_key_fd)
    if any(value is None for value in required):
        raise ValueError("signing mode requires canonical bytes, manifest output, signature output, and --approval-key-fd")
    if test_seam:
        if args.release_sha_from_git:
            raise ValueError("test-seam mode rejects --release-sha-from-git")
        source = absolute(args.canonical_approval_bytes, name="canonical approval")
        manifest = absolute(args.approval_manifest, name="approval manifest")
        signature = absolute(args.approval_signature, name="approval signature")
    else:
        if not args.release_sha_from_git:
            raise ValueError("production mode requires --release-sha-from-git")
        if "RELEASE_SHA" in os.environ:
            raise ValueError("ambient RELEASE_SHA is not accepted in production approval mode")
        source, manifest, signature = _validate_production_layout(args)
    value, raw = _read_canonical(source)
    expected_mode = "test-seam" if test_seam else "production"
    _validate_approval(value, signing_mode=expected_mode)
    if not test_seam and value["release_sha"] != release_sha_from_git():
        raise ValueError("canonical approval release SHA does not match exact git HEAD")
    _publish(manifest, signature, raw, key_fd=args.approval_key_fd)
    return 0


def fixture_approval(args: argparse.Namespace) -> int:
    if args.canonical_approval_bytes is None or args.approval_manifest is None or args.approval_signature is None:
        raise ValueError("fixture mode requires canonical bytes, approval manifest, and approval signature")
    raw = _capture_bytes(args.canonical_approval_bytes, name="canonical approval")
    manifest = absolute(args.approval_manifest, name="approval manifest")
    signature = absolute(args.approval_signature, name="approval signature")
    _publish(manifest, signature, raw, signature_bytes=b"fixture-approval-signature\n")
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
    parser.add_argument("--release-sha-from-git", action="store_true")
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
            if args.approval_key_fd is not None or args.release_sha_from_git:
                raise ValueError("fixture mode rejects signing options")
            return fixture_approval(args)
        return sign_approval(args, test_seam=test_seam)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
