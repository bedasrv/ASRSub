"""Small standard-library-only fixture harness shared by hardening tests."""
from __future__ import annotations

import json
import hashlib
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


def require_regular(path: Path) -> None:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or path.is_symlink():
        raise AssertionError(f"not a regular non-symlink fixture: {path}")


def load_json(path: Path) -> dict:
    require_regular(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected object: {path}")
    return value


def run_selector(selector: str, selectors: dict[str, callable]) -> None:
    if selector not in selectors:
        raise SystemExit(f"unknown selector: {selector}")
    selectors[selector]()
    print(f"PASS {selector}")
    print("executed=1 failures=0")


def reject_forbidden_text(text: str, forbidden: tuple[str, ...] = ()) -> None:
    for value in forbidden:
        if value and value in text:
            raise AssertionError(f"forbidden value in fixture output: {value}")


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def domain_hash(domain: str, value: bytes) -> str:
    return sha256_bytes(domain.encode("ascii") + b"\0" + value)


def atomic_json(path: Path, value: object) -> bytes:
    data = canonical(value) + b"\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return data


@contextmanager
def disposable_root(prefix: str):
    with tempfile.TemporaryDirectory(prefix=f"asrsub-{prefix}-") as raw:
        yield Path(raw)
