#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,sys,subprocess
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector
ROOT=Path(__file__).resolve().parent
def schema(): assert json.loads((ROOT/'receipt_schema.json').read_text())['schema']=='receipt-schema-v1'
def reject():
    for path in Path("tests/fixtures").rglob("*.json"):
        text=path.read_text(errors="ignore")
        assert "DISCORD_WEBHOOK_URL=" not in text and "Authorization: Bearer" not in text
def final():
    value=json.loads((ROOT/'acceptance-manifest.json').read_text())
    assert value['schema']=='acceptance-manifest-v1'; assert len(value['core_manifest_hash'])==64
def tracked():
    required=["tools/asrsub-env","tools/check_file_budgets.py","tests/fixtures/platform/planning-receipt.json","tests/fixtures/core_acceptance/acceptance-manifest.json"]
    tracked=set(subprocess.check_output(["git","ls-files","--error-unmatch",*required],text=True).splitlines())
    assert tracked==set(required)
def main():
 p=argparse.ArgumentParser();p.add_argument('selector');a=p.parse_args();run_selector(a.selector,{'test_receipt_schema':schema,'test_rejects_secret_and_real_network':reject,'test_final_receipt_schema':final,'test_all_required_artifacts_tracked':tracked})
if __name__=='__main__':main()
