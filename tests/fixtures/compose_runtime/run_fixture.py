#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector

ROOT=Path(__file__).resolve().parent
COMPOSE=(ROOT/"compose.yaml").read_text()
def enabled():
    assert "/run/secrets/discord_webhook" in COMPOSE and "/run/secrets/control_api_key" in COMPOSE
    assert "user: \"1000:1000\"" in COMPOSE and "cap_drop: [ALL]" in COMPOSE
    assert "DISCORD_WEBHOOK_URL" not in COMPOSE
def disabled():
    assert (ROOT/"empty-secret-source").read_bytes()==b""
    assert "DISCORD_WEBHOOK_URL" not in (ROOT/"empty-secret-source").read_text()
def mounts():
    assert "/home/user/.config" not in COMPOSE and "/var/lib/asrsub/runtime-secrets" in COMPOSE
    assert COMPOSE.count("/run/secrets/")==2
def main():
 p=argparse.ArgumentParser();p.add_argument("selector");a=p.parse_args();run_selector(a.selector,{"test_enabled_secret_projection":enabled,"test_disabled_projection_has_no_url":disabled,"test_compose_has_no_broad_secret_mount":mounts})
if __name__=="__main__":main()
