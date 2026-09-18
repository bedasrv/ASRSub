#!/usr/bin/env python3
"""Render the fixed interpolation-free local Compose projection."""
from __future__ import annotations
import hashlib, json, re
from pathlib import Path

MOUNTS = [
    ("/var/lib/asrsub/config", "/home/user/.config/asr-pipeline", "bind", False),
    ("/var/lib/asrsub/cache", "/home/user/.cache/asr-pipeline", "bind", False),
    ("/var/lib/asrsub/state", "/var/lib/asrsub/state", "bind", False),
    ("/mnt/nas/share/media", "/mnt/nas/share/media", "bind", False),
    ("/mnt/nas/share/media", "/media", "bind", True),
    ("/var/lib/asrsub/runtime-secrets/discord_webhook", "/run/secrets/discord_webhook", "secret", True),
    ("/sys/fs/cgroup/system.slice/asrsub-runtime.service/asrsub-children", "/run/asrsub/children-cgroup", "cgroup", False),
    ("/usr/local/libexec/asrsub/asrsub", "/usr/local/bin/asrsub", "bind", True),
    ("/var/lib/asrsub/egress-policy/egress-policy.json", "/run/asrsub/egress-policy.json", "bind", True),
]

def render(template: Path, image: str) -> bytes:
    text = template.read_text(encoding="utf-8")
    if "${" not in text: raise ValueError("Compose template must contain interpolation inputs")
    if not re.fullmatch(r"ghcr\.io/bedasrv/asrsub@sha256:[0-9a-f]{64}", image): raise ValueError("image must be an immutable digest")
    out = text.replace("${ASRSUB_IMAGE:?ASRSUB_IMAGE must be an immutable digest}", image)
    out = re.sub(r"\$\{PROVIDER_KEYS_FILE:-[^}]+\}", "/var/lib/asrsub/config/provider_keys.env", out)
    out = re.sub(r"\$\{WEBHOOK_PORT:-[^}]+\}", "8085", out)
    if "${" in out: raise ValueError("unresolved Compose interpolation")
    return out.encode()

def main() -> int:
    import argparse
    p=argparse.ArgumentParser(); p.add_argument("--template",type=Path,default=Path("docker-compose.yml")); p.add_argument("--image",required=True); p.add_argument("--output",type=Path,required=True); args=p.parse_args()
    data=render(args.template,args.image); args.output.write_bytes(data)
    print(json.dumps({"schema":"compose-provenance-v1","compose_sha256":hashlib.sha256(data).hexdigest(),"mount_count":len(MOUNTS)},separators=(",",":")))
    return 0
if __name__ == "__main__": raise SystemExit(main())
