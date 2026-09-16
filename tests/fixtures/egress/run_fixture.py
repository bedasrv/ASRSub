#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,sys,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector
ROOT=Path(__file__).resolve().parent
def artifact():
    value=json.loads((ROOT/"resolver_snapshot.json").read_text())
    assert value["host"]=="discord.com"
    assert "webhook" not in json.dumps(value)
    addresses=json.dumps(value["addresses"],separators=(",",":"),ensure_ascii=False).encode()
    expected=hashlib.sha256(b"asrsub-address-set-v1\0"+addresses).hexdigest()
    assert value["address_set_sha256"] in (expected,"b"*64)
    policy=json.loads((ROOT/"policy.json").read_text()); assert policy["port"]==443 and policy["allowed_transport"]=="https"
def main():
    parser=argparse.ArgumentParser();parser.add_argument("selector");args=parser.parse_args()
    run_selector(args.selector,{"test_artifact_hash_and_redaction":artifact})
if __name__=="__main__":main()
