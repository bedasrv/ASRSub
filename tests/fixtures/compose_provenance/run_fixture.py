#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector
ROOT=Path(__file__).resolve().parent
def digest():
 v=json.loads((ROOT/"image-inspect.json").read_text()); assert v["image_ref"].startswith("ghcr.io/bedasrv/asrsub@sha256:")
def rendered(): assert json.loads((ROOT/"rendered.json").read_text())["mount_count"]==10
def main():
 p=argparse.ArgumentParser();p.add_argument("selector");a=p.parse_args();run_selector(a.selector,{"test_digest_only_provenance":digest,"test_rendered_compose_hash":rendered})
if __name__=="__main__":main()
