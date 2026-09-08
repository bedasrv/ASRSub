#!/usr/bin/env python3
"""COMET scoring (wmt22-comet-da) on the FLORES-200 sample MT output.

Usage: ~/benchmark/venvs/eval/bin/python benchmarks/eval_comet.py [--cpu]
"""

import argparse
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(HERE, "data", "flores_sample")
RESULTS_DIR = os.path.join(HERE, "results")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cpu", action="store_true", help="force CPU scoring")
    ap.add_argument("--sys", default=os.path.join(RESULTS_DIR, "mt_hy_mt15_flores200.sys.txt"),
                    help="hypothesis file (one line per source line)")
    ap.add_argument("--out", default=os.path.join(RESULTS_DIR, "comet_flores300.json"))
    args = ap.parse_args()

    from comet import download_model, load_from_checkpoint

    src = open(os.path.join(SAMPLE_DIR, "devtest.sample.ja.txt"), encoding="utf-8").read().splitlines()
    ref = open(os.path.join(SAMPLE_DIR, "devtest.sample.ind.txt"), encoding="utf-8").read().splitlines()
    sys_out = open(args.sys, encoding="utf-8").read().splitlines()
    assert len(src) == len(ref) == len(sys_out)

    t0 = time.time()
    model_path = download_model("Unbabel/wmt22-comet-da")
    model = load_from_checkpoint(model_path)
    if args.cpu:
        model = model.to("cpu")
    model.eval()
    print(f"model load: {time.time() - t0:.0f}s, device: {next(model.parameters()).device}")

    t0 = time.time()
    data = [{"src": s, "mt": m, "ref": r} for s, m, r in zip(src, sys_out, ref)]
    outputs = model.predict(data, batch_size=16, gpus=0 if args.cpu else 1, progress_bar=True)
    elapsed = time.time() - t0
    scores = outputs["scores"]

    result = {
        "task": "FLORES-200 devtest jpn_Jpan->ind_Latn (300-line sample)",
        "model": "wmt22-comet-da",
        "sys": os.path.basename(args.sys),
        "device": "cpu" if args.cpu else "gpu",
        "comet_mean": round(sum(scores) / len(scores), 4),
        "comet_segments": scores,
        "elapsed_s": round(elapsed, 1),
    }
    out = args.out
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print("=" * 60)
    print(f"lines: {len(sys_out)}  elapsed: {elapsed:.0f}s")
    print(f"COMET (wmt22-comet-da): {result['comet_mean']:.4f}")
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
