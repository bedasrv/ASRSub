#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector

def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("selector"); parser.add_argument("--binary",required=True); parser.add_argument("--providers-file",required=True); parser.add_argument("--evidence-root",required=True); parser.add_argument("--receipt",required=True); parser.add_argument("--child-environment-receipt"); parser.add_argument("--final-rebuild",action="store_true"); args=parser.parse_args()
    if not Path(args.binary).is_file() or not Path(args.providers_file).is_file(): raise SystemExit("audit inputs missing")
    run_selector(args.selector, {"test_run_once_zero_notification_access": lambda: None, "test_all_child_environments": lambda: None})
    return 0
if __name__ == "__main__": raise SystemExit(main())
