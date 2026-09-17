#!/usr/bin/env python3
"""Small fail-closed primitives shared by the production adapters."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_DIGEST_RE = re.compile(r"ghcr\.io/bedasrv/asrsub@sha256:[0-9a-f]{64}\Z")


class AdapterError(RuntimeError):
    """A validation or bounded command failure which must stop an adapter."""


def require_hex(value: str, *, name: str, length: int) -> str:
    pattern = COMMIT_RE if length == 40 else HEX64_RE
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise AdapterError(f"{name} must be exactly {length} lowercase hexadecimal characters")
    return value


def require_image_digest(value: str) -> str:
    if not isinstance(value, str) or not IMAGE_DIGEST_RE.fullmatch(value):
        raise AdapterError("image must be an immutable ghcr.io/bedasrv/asrsub@sha256 digest")
    return value


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as exc:
        raise AdapterError(f"cannot encode canonical JSON: {type(exc).__name__}") from exc


def require_absolute(path: Path, *, name: str) -> Path:
    candidate = Path(os.fspath(path))
    if any(part == ".." for part in candidate.parts):
        raise AdapterError(f"{name} contains a parent traversal component")
    if not candidate.is_absolute():
        raise AdapterError(f"{name} must be an absolute path")
    return candidate


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _parts(path: Path) -> list[Path]:
    absolute = _absolute(path)
    current = Path(absolute.anchor)
    result: list[Path] = []
    for part in absolute.parts:
        if part == absolute.anchor:
            continue
        current /= part
        result.append(current)
    return result


def ensure_no_symlink(path: Path, *, name: str, allow_missing: bool = True) -> None:
    """Reject symlinks in every existing component without resolving the path."""
    path = require_absolute(path, name=name)
    absolute = _absolute(path)
    for component in _parts(absolute):
        try:
            st = os.lstat(component)
        except FileNotFoundError:
            if allow_missing:
                return
            raise AdapterError(f"{name} is missing: {path}") from None
        except OSError as exc:
            raise AdapterError(f"cannot inspect {name}: {exc.strerror or exc}") from exc
        if stat.S_ISLNK(st.st_mode):
            raise AdapterError(f"{name} contains a symlink: {component}")
        if component != absolute and not stat.S_ISDIR(st.st_mode):
            raise AdapterError(f"{name} has a non-directory parent: {component}")


def _check_metadata(path: Path, *, mode: int | None, uid: int | None, gid: int | None, name: str) -> os.stat_result:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise AdapterError(f"{name} is missing: {path}") from None
    except OSError as exc:
        raise AdapterError(f"cannot inspect {name}: {exc.strerror or exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise AdapterError(f"{name} is a symlink: {path}")
    if mode is not None and stat.S_IMODE(st.st_mode) != mode:
        raise AdapterError(f"{name} has mode {stat.S_IMODE(st.st_mode):04o}, expected {mode:04o}: {path}")
    if uid is not None and st.st_uid != uid:
        raise AdapterError(f"{name} has uid {st.st_uid}, expected {uid}: {path}")
    if gid is not None and st.st_gid != gid:
        raise AdapterError(f"{name} has gid {st.st_gid}, expected {gid}: {path}")
    return st


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise AdapterError(f"cannot open directory for fsync: {exc.strerror or exc}") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise AdapterError(f"directory fsync failed: {exc.strerror or exc}") from exc
    finally:
        os.close(fd)


def ensure_directory(path: Path, *, mode: int, uid: int | None, gid: int | None, name: str) -> Path:
    path = require_absolute(path, name=name)
    if mode & ~0o777:
        raise AdapterError(f"{name} mode contains non-permission bits")
    ensure_no_symlink(path, name=name)
    absolute = _absolute(path)
    created_target = False
    for component in _parts(absolute):
        try:
            st = os.lstat(component)
        except FileNotFoundError:
            try:
                os.mkdir(component, 0o755 if component != absolute else mode)
            except FileExistsError:
                st = os.lstat(component)
            except OSError as exc:
                raise AdapterError(f"cannot create {name}: {exc.strerror or exc}") from exc
            else:
                st = os.lstat(component)
                if component == absolute:
                    created_target = True
        except OSError as exc:
            raise AdapterError(f"cannot inspect {name}: {exc.strerror or exc}") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise AdapterError(f"{name} has an unsafe directory component: {component}")
    if created_target:
        try:
            os.chmod(absolute, mode, follow_symlinks=False)
            if uid is not None or gid is not None:
                os.chown(absolute, -1 if uid is None else uid, -1 if gid is None else gid)
            _fsync_directory(absolute.parent)
        except OSError as exc:
            raise AdapterError(f"cannot finalize {name}: {exc.strerror or exc}") from exc
    _check_metadata(absolute, mode=mode, uid=uid, gid=gid, name=name)
    return absolute


def ensure_existing_directory(path: Path, *, name: str) -> Path:
    path = require_absolute(path, name=name)
    ensure_no_symlink(path, name=name, allow_missing=False)
    st = _check_metadata(path, mode=None, uid=None, gid=None, name=name)
    if not stat.S_ISDIR(st.st_mode):
        raise AdapterError(f"{name} is not a directory: {path}")
    return path


def ensure_directory_metadata(path: Path, *, mode: int, uid: int | None, gid: int | None, name: str) -> Path:
    """Validate an existing directory without creating or following it."""
    path = require_absolute(path, name=name)
    ensure_no_symlink(path, name=name, allow_missing=False)
    st = _check_metadata(path, mode=mode, uid=uid, gid=gid, name=name)
    if not stat.S_ISDIR(st.st_mode):
        raise AdapterError(f"{name} is not a directory: {path}")
    return path


def ensure_regular_file(path: Path, *, mode: int, uid: int | None, gid: int | None, name: str) -> Path:
    path = require_absolute(path, name=name)
    ensure_no_symlink(path, name=name, allow_missing=False)
    st = _check_metadata(path, mode=mode, uid=uid, gid=gid, name=name)
    if not stat.S_ISREG(st.st_mode):
        raise AdapterError(f"{name} is not a regular file: {path}")
    return path


def ensure_parent_directory(path: Path, *, name: str) -> Path:
    path = require_absolute(path, name=name)
    parent = path.parent
    ensure_no_symlink(parent, name=f"{name} parent", allow_missing=False)
    st = _check_metadata(parent, mode=None, uid=None, gid=None, name=f"{name} parent")
    if not stat.S_ISDIR(st.st_mode):
        raise AdapterError(f"{name} parent is not a directory: {parent}")
    return parent


def create_empty_file(path: Path, *, mode: int, uid: int | None, gid: int | None, name: str) -> Path:
    path = require_absolute(path, name=name)
    ensure_parent_directory(path, name=name)
    ensure_no_symlink(path, name=name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, mode)
    except FileExistsError:
        return ensure_regular_file(path, mode=mode, uid=uid, gid=gid, name=name)
    except OSError as exc:
        raise AdapterError(f"cannot create {name}: {exc.strerror or exc}") from exc
    try:
        os.fchmod(fd, mode)
        if uid is not None or gid is not None:
            os.fchown(fd, -1 if uid is None else uid, -1 if gid is None else gid)
        os.fsync(fd)
    except OSError as exc:
        raise AdapterError(f"cannot finalize {name}: {exc.strerror or exc}") from exc
    finally:
        os.close(fd)
    _fsync_directory(path.parent)
    return ensure_regular_file(path, mode=mode, uid=uid, gid=gid, name=name)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int, uid: int | None, gid: int | None, name: str) -> Path:
    path = require_absolute(path, name=name)
    parent = ensure_parent_directory(path, name=name)
    ensure_no_symlink(path, name=name)
    fd = -1
    temp_path: Path | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
        temp_path = Path(temporary)
        os.fchmod(fd, mode)
        if uid is not None or gid is not None:
            os.fchown(fd, -1 if uid is None else uid, -1 if gid is None else gid)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        temp_path = None
        _fsync_directory(parent)
    except (OSError, UnicodeEncodeError) as exc:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise AdapterError(f"atomic write failed for {name}: {type(exc).__name__}") from exc
    return _check_metadata(path, mode=mode, uid=uid, gid=gid, name=name) and path


def atomic_write_json(path: Path, value: Any, *, mode: int, uid: int | None, gid: int | None, name: str) -> Path:
    return atomic_write_bytes(path, canonical_json(value) + b"\n", mode=mode, uid=uid, gid=gid, name=name)


def read_json(path: Path, *, name: str) -> Any:
    path = require_absolute(path, name=name)
    ensure_no_symlink(path, name=name, allow_missing=False)
    try:
        raw = path.read_bytes()
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(f"cannot read {name}: {type(exc).__name__}") from exc


def sha256_file(path: Path, *, name: str) -> str:
    path = require_absolute(path, name=name)
    ensure_no_symlink(path, name=name, allow_missing=False)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise AdapterError(f"cannot hash {name}: {exc.strerror or exc}") from exc
    return digest.hexdigest()


def _decode_mount_field(value: str) -> str:
    return value.replace(r"\040", " ").replace(r"\011", "\t").replace(r"\134", "\\")


def _mount_identity(path: Path) -> tuple[int | None, str | None]:
    target = str(_absolute(path))
    best: tuple[int | None, str | None, int] = (None, None, -1)
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None, None
    for line in lines:
        fields = line.split()
        if len(fields) < 10 or "-" not in fields[6:]:
            continue
        try:
            mount_id = int(fields[0])
        except ValueError:
            continue
        mountpoint = _decode_mount_field(fields[4])
        prefix = mountpoint.rstrip("/") or "/"
        if target != prefix and not target.startswith(prefix + "/"):
            continue
        if len(prefix) <= best[2]:
            continue
        separator = fields.index("-", 6)
        filesystem = fields[separator + 1] if separator + 1 < len(fields) else None
        best = (mount_id, filesystem, len(prefix))
    return best[0], best[1]


def filesystem_identity(path: Path, *, name: str, allow_unobservable_mount: bool = False) -> dict[str, Any]:
    path = require_absolute(path, name=name)
    ensure_no_symlink(path, name=name, allow_missing=False)
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise AdapterError(f"cannot stat {name}: {exc.strerror or exc}") from exc
    mount_id, filesystem = _mount_identity(path)
    identity: dict[str, Any] = {
        "device": int(st.st_dev),
        "inode": int(st.st_ino),
        "mount_id": mount_id,
        "filesystem": filesystem,
    }
    if identity["device"] <= 0 or identity["inode"] <= 0:
        raise AdapterError(f"{name} has unusable filesystem identity")
    if mount_id is None or mount_id <= 0:
        if not allow_unobservable_mount:
            raise AdapterError(f"{name} has no observable mount identity")
        # Test-seam receipts are explicitly ineligible for production
        # evidence.  Keep their identity shape useful for fixture assertions
        # without weakening the default production check above.
        identity["mount_id"] = identity["device"]
        identity["filesystem"] = "fixture"
    if not filesystem:
        if not allow_unobservable_mount:
            raise AdapterError(f"{name} has no observable filesystem type")
        identity["filesystem"] = "fixture"
    return identity


def bounded(value: str, limit: int = 4096) -> str:
    value = str(value)
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


_SECRET_ASSIGNMENT = re.compile(
    r"(?ix)([\"']?[A-Z0-9_.-]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|WEBHOOK)[A-Z0-9_.-]*[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}\]]+)"
)
_SECRET_FLAG = re.compile(r"(?ix)(--?(?:api[-_]?key|token|secret|password|credential)\s+)([^\s]+)")
_URL_CREDENTIAL = re.compile(r"(https?://[^/:\s]+:)([^@/\s]+)(@)", re.IGNORECASE)


def redact_text(value: str | os.PathLike[str], *, secret_values: Iterable[str] = ()) -> str:
    redacted = str(value)
    for secret in sorted({str(item) for item in secret_values if item}, key=len, reverse=True):
        redacted = redacted.replace(secret, "<redacted>")
    redacted = _URL_CREDENTIAL.sub(r"\1<redacted>\3", redacted)
    redacted = _SECRET_ASSIGNMENT.sub(r"\1<redacted>", redacted)
    redacted = _SECRET_FLAG.sub(r"\1<redacted>", redacted)
    return bounded(redacted)


def environment_secret_values(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    source = os.environ if env is None else env
    markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "WEBHOOK")
    return tuple(
        value
        for name, value in source.items()
        if value and any(marker in name.upper() for marker in markers)
    )


def reject_ambient_docker_environment(*, production: bool, env: Mapping[str, str] | None = None) -> None:
    source = os.environ if env is None else env
    present = []
    for name, value in source.items():
        upper = name.upper()
        forbidden = upper in {
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "COMPOSE_FILE",
            "COMPOSE_PROJECT_NAME",
            "COMPOSE_PATH_SEPARATOR",
            "COMPOSE_PROFILES",
            "COMPOSE_ENV_FILES",
            "COMPOSE_INTERACTIVE_NO_CLI",
            "PROJECT_NAME",
        }
        if production:
            forbidden = forbidden or upper.startswith("DOCKER_") or upper.startswith("COMPOSE_") or upper.endswith("_PROXY") or upper == "NO_PROXY"
        if forbidden and (value or production):
            present.append(name)
    if present:
        raise AdapterError("forbidden ambient Docker/Compose environment is present")


def production_command_environment() -> dict[str, str]:
    reject_ambient_docker_environment(production=True)
    return {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def run_argv(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    timeout: float = 60.0,
    secret_values: Iterable[str] = (),
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    command = [os.fspath(item) for item in argv]
    try:
        completed = subprocess.run(
            command,
            cwd=None if cwd is None else os.fspath(cwd),
            capture_output=True,
            text=False,
            timeout=timeout,
            check=False,
            shell=False,
            env=None if env is None else dict(env),
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        message = redact_text(str(exc), secret_values=secret_values)
        raise AdapterError(f"command execution failed: {message}") from exc
    try:
        stdout = (completed.stdout or b"").decode("utf-8")
        stderr = (completed.stderr or b"").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdapterError("command output was not valid UTF-8") from exc
    evidence = {
        "argv": [redact_text(item, secret_values=secret_values) for item in command],
        "returncode": int(completed.returncode),
        "stdout": redact_text(stdout, secret_values=secret_values),
        "stderr": redact_text(stderr, secret_values=secret_values),
    }
    if completed.returncode != 0:
        detail = evidence["stderr"] or evidence["stdout"] or "no command output"
        raise AdapterError(f"command returned {completed.returncode}: {detail}")
    return evidence


def now_ns() -> int:
    value = time.time_ns()
    if value <= 0:
        raise AdapterError("clock returned an invalid epoch")
    return value


def default_owner() -> tuple[int, int]:
    return os.geteuid(), os.getegid()
