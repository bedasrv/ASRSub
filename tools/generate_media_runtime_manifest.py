#!/usr/bin/env python3
"""Emit a bounded local media-runtime manifest from a fixed inspect fixture."""
from __future__ import annotations
import json
from pathlib import Path

def main() -> int:
    image = Path("tests/fixtures/run_once_audit/media-runtime-image-inspect.json")
    output = Path("tests/fixtures/run_once_audit/media-runtime-dependencies.json")
    value = json.loads(image.read_text())
    digest = value["image_id"].split(":", 1)[1]
    output.write_text(json.dumps({"schema":"media-runtime-manifest-v1","image_digest":digest,"platform":"linux/amd64","executables":[]}, separators=(",", ":")) + "\n")
    return 0
if __name__ == "__main__": raise SystemExit(main())
