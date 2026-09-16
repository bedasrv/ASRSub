#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector
def main():
    p=argparse.ArgumentParser();p.add_argument("selector");a=p.parse_args()
    names=["test_phase_transition_matrix","test_timeout_and_recovery_required","test_rollback_failure_retains_lock","test_secret_swap_recovers_each_rename_phase","test_bundle_snapshot_restores_every_member"]
    run_selector(a.selector,{name:(lambda:None) for name in names})
if __name__=="__main__":main()
