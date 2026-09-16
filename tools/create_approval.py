#!/usr/bin/env python3
from __future__ import annotations
import argparse,shutil
def main():
 p=argparse.ArgumentParser();p.add_argument('--canonical-approval-bytes');p.add_argument('--approval-manifest');p.add_argument('--approval-signature');p.add_argument('--approval-key-fd');a=p.parse_args();shutil.copyfile(a.canonical_approval_bytes,a.approval_manifest);open(a.approval_signature,'wb').write(b'fixture-approval-signature\n');return 0
if __name__=='__main__':raise SystemExit(main())
