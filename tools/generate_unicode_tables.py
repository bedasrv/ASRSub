#!/usr/bin/env python3
"""Validate the pinned Unicode 15.1 fixture used by the Rust sanitizer."""
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path

FIXTURE = Path(__file__).parents[1] / "tests/fixtures/unicode_15_1/derived_core_properties.json"

def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "--check":
        raise SystemExit("usage: generate_unicode_tables.py --check")
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if value.get("schema") != "unicode-15.1-derived-core-properties-v1":
        raise SystemExit("unicode fixture schema mismatch")
    payload = dict(value)
    payload["fixture_sha256"] = ""
    digest = hashlib.sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    if not isinstance(value.get("fixture_sha256"), str) or value.get("fixture_sha256") != digest:
        raise SystemExit("unicode fixture digest mismatch")
    if not isinstance(value.get("default_ignorable_ranges"), list):
        raise SystemExit("unicode fixture ranges missing")
    print("unicode tables: checked")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
