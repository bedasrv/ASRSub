#!/usr/bin/env python3
"""Fixture-only approval projection; never a production signer."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true", help="allow the disposable fixture projection")
    parser.add_argument("--canonical-approval-bytes", type=Path, required=True)
    parser.add_argument("--approval-manifest", type=Path, required=True)
    parser.add_argument("--approval-signature", type=Path, required=True)
    parser.add_argument("--approval-key-fd")
    args = parser.parse_args(argv)
    if not args.fixture:
        parser.error("this helper is fixture-only; production requires detached /usr/bin/openssl verification")
    shutil.copyfile(args.canonical_approval_bytes, args.approval_manifest)
    args.approval_signature.write_bytes(b"fixture-approval-signature\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
