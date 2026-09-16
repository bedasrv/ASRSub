#!/usr/bin/env python3
"""Local deterministic bundle/provisional approval projections."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

def canonical(value): return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
def emit_image(ref, release, output):
    if not ref.startswith("ghcr.io/bedasrv/asrsub@sha256:") or len(ref.rsplit(":",1)[1]) != 64: raise SystemExit("invalid approved image")
    value={"schema":"approved-image-v1","image_ref":ref,"image_digest":ref.rsplit(":",1)[1],"release_sha":release,"platform":"linux/amd64","created_epoch_ns":0}
    output.write_bytes(canonical(value)+b"\n")
def main():
 p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="mode",required=True)
 a=sub.add_parser("--emit-approved-image",add_help=False); a.add_argument("--image-ref",required=True);a.add_argument("--release-sha",required=True);a.add_argument("--output",type=Path,required=True)
 h=sub.add_parser("--approved-image-hash",add_help=False);h.add_argument("--approved-image",type=Path,required=True)
 args=p.parse_args()
 if args.mode=="--emit-approved-image": emit_image(args.image_ref,args.release_sha,args.output)
 else: print(hashlib.sha256(b"asrsub-approved-image-v1\0"+args.approved_image.read_bytes()).hexdigest())
 return 0
if __name__=="__main__": raise SystemExit(main())
