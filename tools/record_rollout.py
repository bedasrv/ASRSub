#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
def main():
 p=argparse.ArgumentParser();p.add_argument('--fixture');p.add_argument('--output');p.add_argument('--collect-transaction-cgroup',action='store_true');p.add_argument('--collect-target-evidence',action='store_true');p.add_argument('--deployment-root');a=p.parse_args()
 if a.fixture and a.output:json.dump({'schema':'rollout-receipt-v1','kind':'fixture','result':'success','evidence_paths':[]},open(a.output,'w'),separators=(',',':'));open(a.output,'a').write('\n');return 0
 raise SystemExit('local fixture mode requires --fixture and --output')
if __name__=='__main__':raise SystemExit(main())
