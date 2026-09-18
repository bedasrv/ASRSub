#!/usr/bin/env python3
"""Validate the cross-plan path/budget contract.

The manifest is deliberately data-only.  This checker performs the structural
checks before future task files exist and the complete line/import checks once
the implementation has been assembled.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REQUIRED_TOP = {"schema", "entries", "shared_paths", "dag_edges", "task_files"}
ENTRY_KEYS = {
    "path",
    "owner_task",
    "max_lines",
    "max_delta_from_baseline",
    "allowed_direct_production_importers",
    "max_direct_production_importers",
    "waiver",
}
TASK_ROLES = {"create", "modify", "read-only"}


def fail(message: str) -> None:
    raise SystemExit(f"file-budget-check: {message}")


def load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read manifest: {exc}")
    if not isinstance(value, dict) or value.get("schema") != "file-budget-v1":
        fail("schema must be file-budget-v1")
    if not REQUIRED_TOP <= set(value):
        fail("manifest is missing required sections")
    return value


def structural(repo: Path, manifest: dict) -> None:
    entries = manifest["entries"]
    if not isinstance(entries, list) or not entries:
        fail("entries must be non-empty")
    paths: set[str] = set()
    for entry in entries:
        if set(entry) != ENTRY_KEYS:
            fail(f"entry keys drift at {entry.get('path')!r}")
        path = entry["path"]
        if not isinstance(path, str) or not path or path.startswith("/") or "*" in path:
            fail(f"invalid fixed path {path!r}")
        if path in paths:
            fail(f"duplicate entry {path}")
        paths.add(path)
        if not isinstance(entry["max_lines"], int) or entry["max_lines"] <= 0:
            fail(f"invalid line budget for {path}")
        importers = entry["allowed_direct_production_importers"]
        if not isinstance(importers, list) or len(set(importers)) != len(importers):
            fail(f"invalid importer list for {path}")
        if entry["max_direct_production_importers"] != len(importers):
            fail(f"importer count mismatch for {path}")
        if entry["waiver"] not in ("none", "legacy-baseline-importers"):
            fail(f"invalid waiver for {path}")
        if entry["waiver"] == "legacy-baseline-importers" and path != "src/config.rs":
            fail(f"legacy waiver is not permitted for {path}")
        if path.startswith("src/") and entry["max_lines"] > 1000 and path not in {
            "src/api.rs", "src/asr.rs", "src/config.rs", "src/episode.rs", "src/jellyfin.rs",
            "src/lang.rs", "src/main.rs", "src/pipeline.rs", "src/sim.rs", "src/tests.rs",
        }:
            fail(f"new/focused source exceeds 1000 line budget: {path}")

    seen_shared: set[str] = set()
    for shared in manifest["shared_paths"]:
        path = shared.get("path")
        if path in seen_shared or path not in paths:
            fail(f"invalid shared path {path}")
        seen_shared.add(path)
        if len(shared.get("modifiers", [])) != len(set(shared.get("modifiers", []))):
            fail(f"duplicate modifier for {path}")

    edges: set[tuple[str, str, str]] = set()
    for edge in manifest["dag_edges"]:
        key = (edge.get("path"), edge.get("before"), edge.get("after"))
        if key in edges or key[0] not in paths or key[1] == key[2]:
            fail(f"invalid/duplicate DAG edge {key}")
        edges.add(key)
    # A path-level cycle check is enough for the frozen contract.
    by_path: dict[str, dict[str, set[str]]] = {}
    for path, before, after in edges:
        by_path.setdefault(path, {}).setdefault(before, set()).add(after)
    for path, graph in by_path.items():
        def visit(node: str, stack: set[str]) -> None:
            if node in stack:
                fail(f"DAG cycle for {path}")
            for child in graph.get(node, ()):
                visit(child, stack | {node})
        for node in graph:
            visit(node, set())

    task_keys: set[tuple[str, str]] = set()
    for item in manifest["task_files"]:
        key = (item.get("task"), item.get("path"))
        if key in task_keys or key[1] not in paths or item.get("role") not in TASK_ROLES:
            fail(f"invalid/duplicate task file {key}")
        task_keys.add(key)


def full(repo: Path, manifest: dict) -> None:
    structural(repo, manifest)
    for entry in manifest["entries"]:
        path = repo / entry["path"]
        if not path.exists():
            fail(f"declared implementation path is absent: {entry['path']}")
        if path.is_file():
            lines = len(path.read_text(encoding="utf-8", errors="strict").splitlines())
            if lines > entry["max_lines"]:
                fail(f"{entry['path']} has {lines} lines, budget is {entry['max_lines']}")
    print(json.dumps({"schema": "file-budget-receipt-v1", "checked": len(manifest["entries"])}, separators=(",", ":")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--structural-only", action="store_true")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo_root.resolve()
    manifest = load((repo / args.manifest).resolve() if not args.manifest.is_absolute() else args.manifest)
    if args.structural_only:
        structural(repo, manifest)
        print("file-budget-check: structural contract valid")
    else:
        full(repo, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
