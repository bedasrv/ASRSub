#!/usr/bin/env python3
"""Emit a bounded local MediaRuntimeManifestV1 from a fixed inspect fixture."""
from __future__ import annotations
import argparse, json, re
from pathlib import Path

HEX64=re.compile(r"^[0-9a-f]{64}$")

def validate(value: dict, approved: dict|None, require_final: bool) -> dict:
    if value.get("schema") != "image-inspect-v1" or value.get("platform") != "linux/amd64": raise ValueError("invalid image inspect fixture")
    image_ref=value.get("image_ref",""); image_id=value.get("image_id","")
    if not re.fullmatch(r"ghcr\.io/bedasrv/asrsub@sha256:[0-9a-f]{64}", image_ref): raise ValueError("image ref is not an approved digest")
    digest=image_ref.rsplit(":",1)[1]
    if image_id != "sha256:"+digest or value.get("repo_digests") != [image_ref]: raise ValueError("image identity mismatch")
    if require_final:
        if not approved or approved.get("image_ref") != image_ref or approved.get("platform") != "linux/amd64": raise ValueError("approved image mismatch")
        if approved.get("release_sha") != value.get("release_sha"): raise ValueError("release mismatch")
    runtime=value.get("media_runtime")
    if not isinstance(runtime,dict) or not isinstance(runtime.get("executables"),list): raise ValueError("media runtime records missing")
    records=runtime["executables"]
    if {r.get("token") for r in records}!={"ffmpeg","ffprobe"} or len(records)!=2: raise ValueError("exact ffmpeg/ffprobe records required")
    for record in records:
        if record.get("path") != "/usr/bin/"+record["token"] or not HEX64.fullmatch(record.get("sha256","")) or not HEX64.fullmatch(record.get("elf_build_id","")): raise ValueError("executable identity malformed")
        if not isinstance(record.get("dependencies"),list): raise ValueError("dependencies missing")
        for dep in record["dependencies"]:
            if not HEX64.fullmatch(dep.get("sha256","")) or not dep.get("path","").startswith("/"): raise ValueError("dependency malformed")
    return {"schema":"media-runtime-manifest-v1","image_digest":digest,"platform":"linux/amd64","executables":sorted(records,key=lambda r:r["token"]),"landlock_read_tokens":sorted(set(runtime.get("landlock_read_tokens",[])))}

def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--image-inspect",type=Path,default=Path("tests/fixtures/run_once_audit/media-runtime-image-inspect.json")); parser.add_argument("--output",type=Path,default=Path("tests/fixtures/run_once_audit/media-runtime-dependencies.json")); parser.add_argument("--approved-image",type=Path); parser.add_argument("--require-final-image",action="store_true"); args=parser.parse_args()
    value=json.loads(args.image_inspect.read_text()); approved=json.loads(args.approved_image.read_text()) if args.approved_image else None
    manifest=validate(value,approved,args.require_final_image)
    args.output.write_text(json.dumps(manifest,ensure_ascii=False,sort_keys=True,separators=(",", ":"))+"\n")
    return 0
if __name__ == "__main__": raise SystemExit(main())
