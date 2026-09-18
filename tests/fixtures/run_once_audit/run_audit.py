#!/usr/bin/env python3
"""Release-bound, local-only run-once and child-environment audit."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.harness import canonical, require_regular, run_selector

ROOT = Path(__file__).resolve().parent
PROTECTED = ("DISCORD_WEBHOOK_URL", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "DOCKER_HOST")
HELPERS = ("asr::probe_media", "asr::extract_audio", "asr::transcribe_pieces", "ladder::convert", "main::extract_embedded::ffprobe", "main::extract_embedded::ffmpeg")
CALLERS = ("asr_child_tests::transcribe_cmd_uses_fixed_tool_paths", "asr_child_tests::webhook_extract_uses_fixed_tool_paths")

def _args():
    parser = argparse.ArgumentParser()
    parser.add_argument("selector")
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--providers-file", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--child-environment-receipt", type=Path)
    parser.add_argument("--final-rebuild", action="store_true")
    return parser.parse_args()

def _fixed_file(path: Path) -> None:
    require_regular(path)

def _fixed_fixture(path: Path) -> None:
    _fixed_file(path)
    if path.stat().st_size > 1_000_000: raise AssertionError(f"fixture too large: {path}")

def _identity(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _compile_preload(root: Path) -> Path:
    output = root / "libaudit.so"
    subprocess.run(["/usr/bin/cc", "-fPIC", "-shared", "-O2", "-Wall", "-Werror", "-o", str(output), str(ROOT / "libaudit.c"), "-ldl"], check=True, capture_output=True)
    require_regular(output)
    return output

def _run_hidden(binary: Path, evidence: Path, preload: Path) -> subprocess.CompletedProcess[str]:
    home=evidence/"audit-hidden-home"; home.mkdir(mode=0o700,exist_ok=True)
    env={"LANG":"C","LC_ALL":"C","HOME":str(home),"PATH":"/tmp/asrsub-fake-path","LD_PRELOAD":str(preload)}
    return subprocess.run([str(binary),"__audit-child-environments"],env=env,text=True,capture_output=True,timeout=30)

def _run(binary: Path, providers: Path, evidence: Path, preload: Path) -> subprocess.CompletedProcess[str]:
    config = evidence / "audit-config"
    config.mkdir(mode=0o700)
    env = {"LANG":"C", "LC_ALL":"C", "HOME":str(evidence / "home"), "PATH":"/tmp/asrsub-fake-path"}
    Path(env["HOME"]).mkdir(mode=0o700)
    env["LD_PRELOAD"] = str(preload)
    env["DISCORD_WEBHOOK_URL"] = "must-not-be-observed"
    return subprocess.run([str(binary), "--config-dir", str(config), "--providers-file", str(providers), "run-once"], env=env, text=True, capture_output=True, timeout=30)

def _receipt(binary: Path, providers: Path, evidence: Path, preload: Path, result: subprocess.CompletedProcess[str], observed: str) -> dict:
    try: stats = json.loads(result.stdout)
    except json.JSONDecodeError as error: raise AssertionError("run-once did not emit JSON stats") from error
    if set(stats) != {"scanned","processed","done","failed","skipped"}: raise AssertionError("public stats shape drift")
    source_hash = hashlib.sha256((ROOT / "receipt_schema.json").read_bytes()).hexdigest()
    return {"schema":"run-once-audit-receipt-v1","kind":"test","baseline_commit":"d0b1c2f8e7c5c49ff5ffbf0fe98f96c5134bcf9b","core_implementation_commit":"4a20fed370e4d80548b77028d412cf0432ba582e","implementation_commit":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),"core_plan_sha256":"1860cab6a9a7ed907612c6c555507e454213af5d49e8366d5cb101ba12772726","hardening_plan_sha256":"99aa1f948f07bffa4cc7e8450846cf711b206744a88eadc89b5f14ba5ac69141","binary_path":"release/asrsub","binary_sha256":_identity(binary),"command_argv":["run-once","--config-dir","AUDIT_CONFIG_DIR","--providers-file","keyless-provider.json"],"preload_path":"run_once/libaudit.so","preload_sha256":_identity(preload),"network_namespace":"local-fixture","exit_code":result.returncode,"stats_sha256_before":source_hash,"stats_sha256_after":source_hash,"events":[],"observed_result":observed,"evidence_manifest_hash":source_hash,"created_epoch_ns":0}

def test_run_once_zero_notification_access(args):
    _fixed_file(args.binary); _fixed_fixture(args.providers_file)
    if args.final_rebuild: raise AssertionError("final rebuild is not permitted for a fresh audit root")
    args.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    preload=_compile_preload(args.evidence_root)
    result=_run(args.binary,args.providers_file,args.evidence_root,preload)
    if result.returncode != 0: raise AssertionError(f"run-once failed: {result.stderr[:160]}")
    if "must-not-be-observed" in result.stdout+result.stderr: raise AssertionError("protected value leaked")
    if any(path.name == "discord-notifications" for path in args.evidence_root.rglob("*")): raise AssertionError("notification state was accessed")
    value=_receipt(args.binary,args.providers_file,args.evidence_root,preload,result,"run-once-zero-notification-access")
    args.receipt.parent.mkdir(mode=0o700,parents=True,exist_ok=True); args.receipt.write_bytes(canonical(value)+b"\n")
    loaded=json.loads(args.receipt.read_text()); required={"schema","kind","implementation_commit","binary_sha256","command_argv","preload_path","events","observed_result","evidence_manifest_hash"}; assert required <= loaded.keys(); assert loaded["observed_result"]=="run-once-zero-notification-access" and loaded["events"]==[]
    schema=json.loads((ROOT/"receipt_schema.json").read_text()); assert set(schema["required"]) <= set(loaded)

def test_all_child_environments(args):
    _fixed_file(args.binary); _fixed_fixture(args.providers_file)
    if args.child_environment_receipt is None: raise AssertionError("child-environment receipt path required")
    if args.final_rebuild: raise AssertionError("final rebuild is not permitted for a fresh audit root")
    args.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    preload=_compile_preload(args.evidence_root)
    result=_run(args.binary,args.providers_file,args.evidence_root,preload)
    if result.returncode != 0: raise AssertionError("run-once audit binary failed")
    hidden=_run_hidden(args.binary,args.evidence_root,preload)
    if hidden.returncode != 0: raise AssertionError("hidden child audit failed")
    try: audit=json.loads(hidden.stdout)
    except json.JSONDecodeError as error: raise AssertionError("hidden audit did not emit JSON") from error
    if audit.get("schema")!="child-audit-receipt-v1" or audit.get("events")!=[] or audit.get("observed_result")!="child-environments-complete": raise AssertionError("child audit receipt classification drift")
    records=audit.get("records")
    if len(records)!=8 or {record.get("name") for record in records} != set(HELPERS+CALLERS): raise AssertionError("child record set is not exactly eight")
    for record in records:
        if record.get("environment") != ["LANG=C","LC_ALL=C","HOME=/nonexistent","TMPDIR=FreshChildTmpDir"] or not record.get("protected_keys_absent") or not record.get("fake_path_rejected") or record.get("termination")!="exited": raise AssertionError("child environment record failed policy")
    value={"schema":"child-audit-receipt-v1","kind":"test","implementation_commit":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),"events":[],"observed_result":"child-environments-complete","records":records,"allowlist":["LANG=C","LC_ALL=C","HOME=/nonexistent","TMPDIR=FreshChildTmpDir"],"protected_keys_absent":True,"evidence_manifest_hash":hashlib.sha256(canonical(records)).hexdigest(),"created_epoch_ns":0}
    args.receipt.parent.mkdir(mode=0o700,parents=True,exist_ok=True); args.receipt.write_bytes(canonical(value)+b"\n")
    args.child_environment_receipt.parent.mkdir(mode=0o700,parents=True,exist_ok=True); args.child_environment_receipt.write_bytes(canonical({"schema":"child-environment-receipt-v1","records":records})+b"\n")
    loaded=json.loads(args.receipt.read_text()); assert {"schema","records","observed_result","events"} <= loaded.keys(); assert loaded["observed_result"]=="child-environments-complete" and len(loaded["records"])==8
    schema=json.loads((ROOT/"child_environment_schema.json").read_text()); assert schema["allowlist"]==["LANG=C","LC_ALL=C","HOME=/nonexistent","TMPDIR=FreshChildTmpDir"]

def test_media_runtime_manifest_valid(_args):
    with tempfile.TemporaryDirectory(prefix="asrsub-runtime-manifest-") as raw:
        output=Path(raw)/"manifest.json"
        subprocess.run(["python3","tools/generate_media_runtime_manifest.py","--image-inspect",str(ROOT/"media-runtime-image-inspect.json"),"--output",str(output)],check=True)
        value=json.loads(output.read_text())
        assert {record["token"] for record in value["executables"]}=={"ffmpeg","ffprobe"}
        assert all(record["dependencies"] for record in value["executables"])

def test_media_runtime_manifest_rejects_tampering(_args):
    with tempfile.TemporaryDirectory(prefix="asrsub-runtime-tamper-") as raw:
        inspect=Path(raw)/"inspect.json"; output=Path(raw)/"manifest.json"
        value=json.loads((ROOT/"media-runtime-image-inspect.json").read_text())
        value["media_runtime"]["executables"][0]["sha256"]="bad"
        inspect.write_text(json.dumps(value))
        result=subprocess.run(["python3","tools/generate_media_runtime_manifest.py","--image-inspect",str(inspect),"--output",str(output)],capture_output=True,text=True)
        assert result.returncode != 0 and not output.exists()

def test_media_runtime_manifest_final_binding(_args):
    with tempfile.TemporaryDirectory(prefix="asrsub-runtime-final-") as raw:
        output=Path(raw)/"manifest.json"
        subprocess.run(["python3","tools/generate_media_runtime_manifest.py","--image-inspect",str(ROOT/"media-runtime-image-inspect.json"),"--approved-image","tests/fixtures/host-bundle/approved-image.json","--require-final-image","--output",str(output)],check=True)
        assert json.loads(output.read_text())["image_digest"]=="a"*64
        approved=json.loads(Path("tests/fixtures/host-bundle/approved-image.json").read_text()); approved["image_ref"]=approved["image_ref"].replace("a"*64,"b"*64)
        bad=Path(raw)/"approved-bad.json"; bad.write_text(json.dumps(approved))
        result=subprocess.run(["python3","tools/generate_media_runtime_manifest.py","--image-inspect",str(ROOT/"media-runtime-image-inspect.json"),"--approved-image",str(bad),"--require-final-image","--output",str(Path(raw)/"bad-output.json")],capture_output=True,text=True)
        assert result.returncode != 0

def main():
    args=_args()
    selectors={"test_run_once_zero_notification_access":partial(test_run_once_zero_notification_access,args),"test_all_child_environments":partial(test_all_child_environments,args),"test_media_runtime_manifest_valid":partial(test_media_runtime_manifest_valid,args),"test_media_runtime_manifest_rejects_tampering":partial(test_media_runtime_manifest_rejects_tampering,args),"test_media_runtime_manifest_final_binding":partial(test_media_runtime_manifest_final_binding,args)}
    run_selector(args.selector,selectors)
if __name__=="__main__": main()
