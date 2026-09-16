#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.harness import atomic_json, disposable_root, load_json, run_selector

ROOT=Path(__file__).resolve().parent

def test_mutation_inventory_is_complete():
    inventory=load_json(ROOT/"mutation-boundaries.json")
    expected={"api","web","jellyfin","run-once","daemon"}
    assert set(inventory["boundaries"])==expected
    with disposable_root("admission") as root:
        state=root/"admission.json"
        atomic_json(state,{"schema":"admission-v1","mode":"running","generation":0,"active":[]})
        current=load_json(state)
        assert current["mode"]=="running" and current["active"]==[]
        current["mode"]="quiescing"; current["generation"]+=1
        atomic_json(state,current)
        assert load_json(state)["mode"]=="quiescing"
        assert load_json(state)["generation"]==1

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("selector"); args=parser.parse_args()
    run_selector(args.selector,{"test_mutation_inventory_is_complete":test_mutation_inventory_is_complete})
if __name__=="__main__": raise SystemExit(main())
