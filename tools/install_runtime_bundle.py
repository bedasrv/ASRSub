#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path
def main():
 p=argparse.ArgumentParser();p.add_argument("--check-fixture",action="store_true");a=p.parse_args()
 if not a.check_fixture: raise SystemExit("production installation requires authenticated bundle handoff")
 required=[Path("systemd/asrsub-recovery.service"),Path("systemd/asrsub-runtime.service"),Path("systemd/docker.service.d/asrsub-recovery.conf")]
 for path in required:
  if not path.is_file():raise SystemExit(f"missing fixture {path}")
 return 0
if __name__=="__main__":raise SystemExit(main())
