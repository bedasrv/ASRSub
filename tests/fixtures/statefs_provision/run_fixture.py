#!/usr/bin/env python3
from __future__ import annotations
import argparse,tempfile,subprocess,json,sys
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector
def clean():
    with tempfile.TemporaryDirectory(prefix="asrsub-statefs-") as raw:
        root=Path(raw)/"root"; evidence=Path(raw)/"evidence"
        subprocess.run(["python3","tools/provision_statefs.py","--fixture-root",str(root),"--evidence-root",str(evidence)],check=True)
        assert (root/"state.jsonl").is_file(); assert (root/"state.jsonl.lock").is_file()
        assert (root/"discord-notifications/quarantine").is_dir(); assert (root/"discord-notifications/state.json").exists() is False
        admission=json.loads((root/"deployment-admission/admission.json").read_text())
        assert admission["mode"]=="recovery_required" and admission["active"]==[]
        receipt=json.loads((evidence/"statefs-provision/statefs-provision-receipt.json").read_text())
        assert receipt["schema"]=="statefs-provision-receipt-v1"
def main():
 p=argparse.ArgumentParser();p.add_argument("selector");a=p.parse_args();run_selector(a.selector,{"test_clean_statefs_is_provisioned":clean})
if __name__=="__main__":main()
