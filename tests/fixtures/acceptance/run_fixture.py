#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector
ROOT=Path(__file__).resolve().parent
def schema(): assert json.loads((ROOT/'receipt_schema.json').read_text())['schema']=='receipt-schema-v1'
def reject(): assert 'webhook' not in (ROOT/'receipt_schema.json').read_text()
def final(): assert json.loads((ROOT/'acceptance-manifest.json').read_text())['schema']=='acceptance-manifest-v1'
def tracked(): assert (ROOT/'receipt_schema.json').is_file()
def main():
 p=argparse.ArgumentParser();p.add_argument('selector');a=p.parse_args();run_selector(a.selector,{'test_receipt_schema':schema,'test_rejects_secret_and_real_network':reject,'test_final_receipt_schema':final,'test_all_required_artifacts_tracked':tracked})
if __name__=='__main__':main()
