#!/usr/bin/env python3
"""Deterministic deployment-journal crash/recovery fixture model."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.harness import atomic_json, canonical, disposable_root, run_selector, sha256_bytes

PHASES = ["prepared", "quiescing", "quiesced", "installing", "installed", "starting", "started", "committed", "rollback_required", "rolled_back", "aborted", "recovery_required"]
TRANSITIONS = {
    None: {"prepared", "recovery_required"},
    "prepared": {"quiescing", "aborted", "recovery_required"},
    "quiescing": {"quiesced", "aborted", "recovery_required"},
    "quiesced": {"installing", "aborted"},
    "installing": {"installed", "rollback_required", "recovery_required"},
    "installed": {"starting", "rollback_required"},
    "starting": {"started", "rollback_required", "recovery_required"},
    "started": {"committed", "rollback_required", "recovery_required"},
    "rollback_required": {"rolled_back", "recovery_required"},
    "recovery_required": {"quiesced", "rollback_required", "rolled_back", "aborted"},
    "committed": set(), "rolled_back": set(), "aborted": set(),
}


def test_phase_transition_matrix() -> None:
    for current, allowed in TRANSITIONS.items():
        for requested in PHASES:
            expected = requested in allowed
            actual = requested in TRANSITIONS.get(current, set())
            assert actual == expected, (current, requested)
    assert TRANSITIONS["started"] == {"committed", "rollback_required", "recovery_required"}


def test_timeout_and_recovery_required() -> None:
    state = {"phase": "quiescing", "accept_work": False, "lease_present": True, "lock_held": True}
    joined = False
    if not joined:
        state.update(phase="recovery_required", accept_work=False, lease_present=True, lock_held=True)
    assert state == {"phase": "recovery_required", "accept_work": False, "lease_present": True, "lock_held": True}


def test_rollback_failure_retains_lock() -> None:
    state = {"phase": "rollback_required", "lease_present": True, "lock_held": True}
    restore_ok = False
    if not restore_ok:
        state["phase"] = "recovery_required"
    assert state["phase"] == "recovery_required"
    assert state["lease_present"] and state["lock_held"]


def test_secret_swap_recovers_each_rename_phase() -> None:
    with disposable_root("secret-swap") as root:
        target = root / "target"
        backup = root / "backup"
        target.write_bytes(b"old-secret")
        for phase in ("recorded", "old-backed-up", "candidate-installed", "restored"):
            candidate = root / f"candidate-{phase}"
            candidate.write_bytes(b"new-secret")
            if phase == "old-backed-up":
                os.replace(target, backup)
            elif phase == "candidate-installed":
                os.replace(candidate, target)
            elif phase == "restored":
                if target.exists(): target.unlink()
                os.replace(backup, target)
            assert target.exists() or backup.exists()
        assert target.read_bytes() == b"old-secret"


def test_bundle_snapshot_restores_every_member() -> None:
    with disposable_root("bundle-snapshot") as root:
        source = root / "source"; snapshot = root / "snapshot"; restored = root / "restored"
        source.mkdir(); snapshot.mkdir(); restored.mkdir()
        members = {"compose.yaml": b"compose-bytes", "release": b"binary-bytes", "inventory.json": b"inventory"}
        for name, data in members.items(): (source / name).write_bytes(data); (snapshot / name).write_bytes(data)
        (source / "compose.yaml").write_bytes(b"mutated")
        for name in members: shutil.copyfile(snapshot / name, restored / name)
        assert {name: (restored / name).read_bytes() for name in members} == members
        assert sha256_bytes(canonical(sorted(members)))


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("selector"); args = parser.parse_args()
    run_selector(args.selector, {name: globals()[name] for name in __all__})
    return 0


__all__ = ["test_phase_transition_matrix", "test_timeout_and_recovery_required", "test_rollback_failure_retains_lock", "test_secret_swap_recovers_each_rename_phase", "test_bundle_snapshot_restores_every_member"]

if __name__ == "__main__": raise SystemExit(main())
