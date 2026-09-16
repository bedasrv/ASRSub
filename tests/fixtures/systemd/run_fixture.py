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
    staged=ROOT/"staged/usr/local/libexec/asrsub"
    for member in value["members"]:
        path=staged/member
        if member=="media-runtime-dependencies.json": assert path.is_file()
        else: assert path.is_file() and os.access(path,os.X_OK)
def rollback():
    inventory=json.loads((ROOT/"runtime-bundle-inventory.json").read_text())
    assert inventory["cgroup"].endswith("asrsub-children")
    assert "Delegate=yes" in (ROOT/"etc/systemd/system/asrsub-runtime.service").read_text()
def main():
 p=argparse.ArgumentParser();p.add_argument("selector");a=p.parse_args();run_selector(a.selector,{"test_recovery_precedes_runtime":recovery,"test_provisional_bundle_contains_units_and_dropin":bundle,"test_rollback_requires_post_restore_cgroup_projection":rollback})
if __name__=="__main__":main()
