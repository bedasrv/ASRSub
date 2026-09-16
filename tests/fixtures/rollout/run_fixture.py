#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, subprocess, tempfile
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import atomic_json, disposable_root, load_json, run_selector

def _receipt():
    with disposable_root("rollout") as root:
        fixture=root/"fixture.json"; output=root/"receipt.json"
        atomic_json(fixture,{"schema":"target-fixture-v1","health":{"healthy":True},"secret_projection":{"notifications_enabled":False},"state_root":{"mode":"running","generation":1}})
        subprocess.run(["python3","tools/record_rollout.py","--fixture",str(fixture),"--output",str(output)],check=True)
        value=load_json(output)
        assert value["schema"]=="rollout-receipt-v1" and value["result"]=="success"
        assert value["evidence_paths"]==[]

def test_rollout_receipt_binding(): _receipt()
def test_transaction_cgroup_projection_producer():
    with disposable_root("cgroup-evidence") as root:
        projection={"schema":"cgroup-projection-v1","controllers":["cpu","memory","pids"],"zero_before":True,"zero_after_cancel":True}
        path=root/"cgroup-projection.json"; atomic_json(path,projection)
        assert load_json(path)["controllers"]==["cpu","memory","pids"]

def main():
    parser=argparse.ArgumentParser();parser.add_argument("selector");args=parser.parse_args()
    run_selector(args.selector,{"test_rollout_receipt_binding":test_rollout_receipt_binding,"test_transaction_cgroup_projection_producer":test_transaction_cgroup_projection_producer})
if __name__=="__main__":raise SystemExit(main())
