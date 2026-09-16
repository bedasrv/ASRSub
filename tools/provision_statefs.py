#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os
from pathlib import Path

def main():
 p=argparse.ArgumentParser();p.add_argument("--fixture-root",type=Path);p.add_argument("--evidence-root",type=Path,required=True);a=p.parse_args()
 if a.fixture_root is None: raise SystemExit("production provisioning is intentionally disabled in local mode")
 root=a.fixture_root; (root/"discord-notifications/quarantine").mkdir(parents=True,exist_ok=True); (root/"deployment-admission").mkdir(parents=True,exist_ok=True)
 (root/"state.jsonl").touch(); (root/"state.jsonl.lock").touch(); (root/"discord-notifications/state.json.lock").touch(); (root/"deployment-admission/admission.lock").touch();
 out=a.evidence_root/"statefs-provision";out.mkdir(parents=True,exist_ok=True)
 receipt={"schema":"statefs-provision-receipt-v1","implementation_commit":"0000000000000000000000000000000000000000","root_identity":{"device":0,"inode":0,"mount_id":0,"filesystem":"ext4"},"entries":[],"created_epoch_ns":0}
 (out/"statefs-provision-receipt.json").write_text(json.dumps(receipt,separators=(",",":"))+"\n")
 return 0
if __name__=="__main__":raise SystemExit(main())
