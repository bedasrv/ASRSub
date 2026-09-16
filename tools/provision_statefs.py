#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, stat
from pathlib import Path

def main():
 p=argparse.ArgumentParser();p.add_argument("--fixture-root",type=Path);p.add_argument("--evidence-root",type=Path,required=True);a=p.parse_args()
 if a.fixture_root is None: raise SystemExit("production provisioning is intentionally disabled in local mode")
 root=a.fixture_root; root.mkdir(parents=True,exist_ok=True); os.chmod(root,0o700)
 (root/"discord-notifications/quarantine").mkdir(parents=True,exist_ok=True); (root/"deployment-admission").mkdir(parents=True,exist_ok=True)
 for path in (root/"state.jsonl",root/"state.jsonl.lock",root/"discord-notifications/state.json.lock",root/"deployment-admission/admission.lock"):
  path.touch(exist_ok=True); os.chmod(path,0o600)
 admission=root/"deployment-admission/admission.json"
 admission.write_text('{"schema":"admission-v1","mode":"recovery_required","generation":0,"active":[],"updated_epoch_ns":0}\n'); os.chmod(admission,0o600)
 out=a.evidence_root/"statefs-provision";out.mkdir(parents=True,exist_ok=True)
 receipt={"schema":"statefs-provision-receipt-v1","implementation_commit":"0000000000000000000000000000000000000000","root_identity":{"device":0,"inode":0,"mount_id":0,"filesystem":"ext4"},"entries":[],"created_epoch_ns":0}
 (out/"statefs-provision-receipt.json").write_text(json.dumps(receipt,separators=(",",":"))+"\n")
 return 0
if __name__=="__main__":raise SystemExit(main())
