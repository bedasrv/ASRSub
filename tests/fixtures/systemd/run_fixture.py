#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector
ROOT=Path(__file__).resolve().parent
def recovery(): assert "Before=docker.service" in (ROOT/"etc/systemd/system/asrsub-recovery.service").read_text()
def bundle(): assert (ROOT/"runtime-bundle-inventory.json").is_file()
def rollback(): assert "cgroup" in (ROOT/"runtime-bundle-inventory.json").read_text()
def main():
 p=argparse.ArgumentParser();p.add_argument("selector");a=p.parse_args();run_selector(a.selector,{"test_recovery_precedes_runtime":recovery,"test_provisional_bundle_contains_units_and_dropin":bundle,"test_rollback_requires_post_restore_cgroup_projection":rollback})
if __name__=="__main__":main()
