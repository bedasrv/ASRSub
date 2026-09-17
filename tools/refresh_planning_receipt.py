#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
def main():
 p=argparse.ArgumentParser();p.add_argument('--receipt',required=True);p.add_argument('--phase',required=True);p.add_argument('--expected-receipt-parent-commit');a=p.parse_args();v=json.load(open(a.receipt));v['phase']=a.phase
 if a.expected_receipt_parent_commit:v['receipt_parent_commit']=a.expected_receipt_parent_commit
 json.dump(v,open(a.receipt,'w'),separators=(',',':'));open(a.receipt,'a').write('\n');return 0
if __name__=='__main__':raise SystemExit(main())
