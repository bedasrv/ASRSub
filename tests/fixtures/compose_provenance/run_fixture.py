#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, subprocess, tempfile, hashlib
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.harness import run_selector
ROOT=Path(__file__).resolve().parent
def digest():
    v=json.loads((ROOT/"image-inspect.json").read_text())
    assert v["image_ref"]==v["repo_digests"][0]
    assert v["image_id"].split(":",1)[1]==v["image_ref"].split(":",1)[1]
    assert v["platform"]=="linux/amd64" and len(v["release_sha"])==40
    with tempfile.TemporaryDirectory(prefix="asrsub-provenance-") as raw:
        out=Path(raw)/"inspect.json"
        subprocess.run(["python3","tools/deploy_docker.py","image-inspect","--fixture-input",str(ROOT/"image-inspect.json"),"--digest",v["image_ref"],"--output",str(out)],check=True)
        assert json.loads(out.read_text())==v
def rendered():
    with tempfile.TemporaryDirectory(prefix="asrsub-compose-") as raw:
        output=Path(raw)/"compose-new.yaml"; digest="ghcr.io/bedasrv/asrsub@sha256:"+"a"*64
        result=subprocess.check_output(["python3","tools/compose_provenance.py","--template","docker-compose.yml","--image",digest,"--output",str(output)],text=True)
        projection=json.loads(result); data=output.read_bytes()
        assert b"${" not in data and projection["mount_count"]==9
        assert projection["compose_sha256"]==hashlib.sha256(data).hexdigest()
def main():
 p=argparse.ArgumentParser();p.add_argument("selector");a=p.parse_args();run_selector(a.selector,{"test_digest_only_provenance":digest,"test_rendered_compose_hash":rendered})
if __name__=="__main__":main()
