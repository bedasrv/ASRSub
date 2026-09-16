"""Small standard-library-only fixture harness shared by hardening tests."""
from __future__ import annotations

import json
import os
import stat
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
