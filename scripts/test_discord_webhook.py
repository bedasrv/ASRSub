#!/usr/bin/env python3
"""Send one explicitly authorized Discord webhook smoke-test message.

This script is separate from the automated test suite. It reads the webhook URL
from a protected file, never accepts it as an argument, never prints it, refuses
redirects and proxies, and reports only the HTTP status.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_SECRET_BYTES = 512
WEBHOOK_PREFIX = "https://discord.com/api/webhooks/"
TRANSPORT_TIMEOUT_SECONDS = 15


class SmokeError(Exception):
    """A safe, non-secret smoke-test failure."""


def default_secret_path() -> Path:
    return Path.home() / ".config" / "asr-pipeline" / "secrets" / "discord_webhook"


def validate_webhook_bytes(value: bytes) -> None:
    if not value or len(value) > MAX_SECRET_BYTES or not value.isascii():
        raise SmokeError("webhook secret is invalid")
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SmokeError("webhook secret is invalid") from exc
    if text != text.strip() or any(char.isspace() or char in "\\\x00" for char in text):
        raise SmokeError("webhook secret is invalid")
    rest = text.removeprefix(WEBHOOK_PREFIX)
    if rest == text or any(char in rest for char in "?#%@"):
        raise SmokeError("webhook secret is invalid")
    pieces = rest.split("/")
    if len(pieces) != 2:
        raise SmokeError("webhook secret is invalid")
    snowflake, token = pieces
    if not 17 <= len(snowflake) <= 20 or not snowflake.isascii() or not snowflake.isdigit():
        raise SmokeError("webhook secret is invalid")
    if not token or len(token) > 256 or token in {".", ".."}:
        raise SmokeError("webhook secret is invalid")
    if not all(char.isascii() and (char.isalnum() or char in "._-") for char in token):
        raise SmokeError("webhook secret is invalid")


def read_secret(path: Path) -> bytes:
    """Read and validate a private regular file without exposing its value."""
    path = path.expanduser()
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SmokeError("webhook secret file is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SmokeError("webhook secret file is not a regular file")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise SmokeError("webhook secret file permissions are unsafe")
    try:
        parent = path.parent.lstat()
    except OSError as exc:
        raise SmokeError("webhook secret directory is unavailable") from exc
    if not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode) or parent.st_mode & 0o077:
        raise SmokeError("webhook secret directory permissions are unsafe")
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise SmokeError("webhook secret file is unreadable") from exc
    validate_webhook_bytes(value)
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SmokeError("webhook redirect refused")


def post_payload(url: str, payload: bytes) -> int:
    """POST a bounded payload with no proxy or redirect and return only status."""
    request = Request(
        url,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ASRSub/3.0.0 webhook-smoke",
        },
        method="POST",
    )
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=TRANSPORT_TIMEOUT_SECONDS)
    except HTTPError as exc:
        raise SmokeError(f"webhook returned HTTP {exc.code}") from exc
    except (OSError, URLError, TimeoutError) as exc:
        raise SmokeError("webhook transport failed") from exc
    try:
        status = int(response.status)
    finally:
        response.close()
    if not 200 <= status <= 299:
        raise SmokeError(f"webhook returned HTTP {status}")
    return status


def build_payload() -> bytes:
    return json.dumps(
        {
            "content": "ASRSub webhook smoke test: one authorized delivery.",
            "allowed_mentions": {"parse": []},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="authorize one real webhook POST; without this flag nothing is sent",
    )
    parser.add_argument(
        "--secret-file",
        type=Path,
        default=default_secret_path(),
        help="protected secret file (default: ~/.config/asr-pipeline/secrets/discord_webhook)",
    )
    args = parser.parse_args(argv)
    if not args.live:
        print("refusing live delivery; pass --live explicitly", file=sys.stderr)
        return 2
    try:
        secret = read_secret(args.secret_file)
        status = post_payload(secret.decode("ascii"), build_payload())
    except SmokeError as exc:
        print(f"discord webhook smoke test failed: {exc}", file=sys.stderr)
        return 1
    print(f"discord webhook smoke test accepted: HTTP {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
