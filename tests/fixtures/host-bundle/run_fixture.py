#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector
ROOT=Path(__file__).resolve().parent
def bundle(): assert json.loads((ROOT/"bundle-manifest.json").read_text())["schema"]=="bundle-manifest-v1"; assert (ROOT/"bundle-manifest.sig").read_bytes()
def approval(): assert json.loads((ROOT/"approval.json").read_text())["schema"]=="approval-v1"; assert (ROOT/"approval.sig").read_bytes()
def main():
 p=argparse.ArgumentParser();p.add_argument("selector");a=p.parse_args();run_selector(a.selector,{"test_bundle_signature_and_inventory":bundle,"test_approval_signature_and_binding":approval})
if __name__=="__main__":main()
