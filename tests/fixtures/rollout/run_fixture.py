#!/usr/bin/env python3
from __future__ import annotations
import argparse,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector
def main():
 p=argparse.ArgumentParser();p.add_argument('selector');a=p.parse_args();run_selector(a.selector,{'test_rollout_receipt_binding':lambda:None,'test_transaction_cgroup_projection_producer':lambda:None})
if __name__=='__main__':main()
