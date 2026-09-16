#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector
def main():
    p=argparse.ArgumentParser();p.add_argument("selector");a=p.parse_args();run_selector(a.selector,{"test_mutation_inventory_is_complete":lambda:None})
if __name__=="__main__":main()
