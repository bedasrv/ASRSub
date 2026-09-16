#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, stat
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector
ROOT=Path(__file__).resolve().parent
def recovery(): assert "Before=docker.service" in (ROOT/"etc/systemd/system/asrsub-recovery.service").read_text()
def bundle():
    value=json.loads((ROOT/"runtime-bundle-inventory.json").read_text())
    assert value["schema"]=="runtime-bundle-inventory-v1" and len(value["members"])==9
    assert set(value["signed_members"]) == set(value["members"]) | set(value["runtime_support_members"]) | set(value["systemd_members"])
    staged=ROOT/"staged/usr/local/libexec/asrsub"
    for member in value["members"]:
        path=staged/member
        if member=="media-runtime-dependencies.json": assert path.is_file() and stat.S_IMODE(path.stat().st_mode)==0o644
        else: assert path.is_file() and os.access(path,os.X_OK) and stat.S_IMODE(path.stat().st_mode)==0o755
    for member in value["runtime_support_members"]:
        path=staged/member
        expected_mode=0o755 if member=="production_entrypoint.py" else 0o644
        assert path.is_file() and stat.S_IMODE(path.stat().st_mode)==expected_mode
    for member in value["systemd_members"]:
        path=ROOT/"etc/systemd/system"/member.removeprefix("systemd/")
        assert path.is_file() and stat.S_IMODE(path.stat().st_mode)==0o644
    assert "/opt/mediastack/asrsub" not in (ROOT/"staged/usr/local/libexec/asrsub/asrsub-recover").read_text()
    assert "health-evidence-v1" in (staged/"asrsub-health-probe").read_text()
def rollback():
    inventory=json.loads((ROOT/"runtime-bundle-inventory.json").read_text())
    assert inventory["cgroup"].endswith("asrsub-children")
    assert "Delegate=yes" in (ROOT/"etc/systemd/system/asrsub-runtime.service").read_text()
    assert "RemainAfterExit=yes" in (ROOT/"etc/systemd/system/asrsub-runtime.service").read_text()
def main():
 p=argparse.ArgumentParser();p.add_argument("selector");a=p.parse_args();run_selector(a.selector,{"test_recovery_precedes_runtime":recovery,"test_provisional_bundle_contains_units_and_dropin":bundle,"test_rollback_requires_post_restore_cgroup_projection":rollback})
if __name__=="__main__":main()
