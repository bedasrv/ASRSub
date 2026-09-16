#!/usr/bin/env python3
"""Fixture-only Docker argv/provenance dispatcher."""
from __future__ import annotations
import argparse, json, re
from pathlib import Path
def main():
 p=argparse.ArgumentParser();p.add_argument("operation");p.add_argument("--fixture-input",type=Path);p.add_argument("--digest",required=True);p.add_argument("--output",type=Path,required=True);args=p.parse_args()
 if args.operation!="image-inspect" or args.fixture_input is None: raise SystemExit("fixture-only image-inspect is required")
 value=json.loads(args.fixture_input.read_text())
 if value.get("image_ref") != args.digest or not re.fullmatch(r"ghcr\.io/bedasrv/asrsub@sha256:[0-9a-f]{64}",args.digest): raise SystemExit("digest mismatch")
 args.output.write_text(json.dumps(value,separators=(",",":"))+"\n")
 return 0
if __name__=="__main__": raise SystemExit(main())
