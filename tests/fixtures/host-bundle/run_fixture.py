#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, hashlib
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector
ROOT=Path(__file__).resolve().parent
def canonical(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
def bundle():
    manifest=json.loads((ROOT/"bundle-manifest.json").read_text()); inventory=json.loads((ROOT/"inventory.json").read_text())
    assert manifest["schema"]=="bundle-manifest-v1" and manifest["signing_mode"]=="fixture" and inventory["schema"]=="inventory-v1"
    signature=(ROOT/"bundle-manifest.sig").read_bytes(); assert signature.startswith(b"fixture-") and len(signature)>8
    assert inventory["image_digest"]=="a"*64
    assert hashlib.sha256(canonical(manifest)).hexdigest()
def approval():
    manifest=json.loads((ROOT/"bundle-manifest.json").read_text()); approval=json.loads((ROOT/"approval.json").read_text())
    expected=hashlib.sha256(b"asrsub-approved-bundle-v1\0"+canonical(manifest)).hexdigest()
    assert approval["schema"]=="approval-v1" and approval["signing_mode"]=="fixture" and approval["bundle_sha256"]==expected
    signature=(ROOT/"approval.sig").read_bytes(); assert signature.startswith(b"fixture-")
def main():
 p=argparse.ArgumentParser();p.add_argument("selector");a=p.parse_args();run_selector(a.selector,{"test_bundle_signature_and_inventory":bundle,"test_approval_signature_and_binding":approval})
if __name__=="__main__":main()
