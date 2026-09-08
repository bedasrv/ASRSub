#!/usr/bin/env python3
"""Prepare a deterministic sample of FLORES-200 devtest (jpn_Jpan -> ind_Latn)."""

import argparse
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
FLORES_DIR = os.path.join(HERE, "data", "flores200", "flores200_dataset", "devtest")
OUT_DIR = os.path.join(HERE, "data", "flores_sample")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    with open(os.path.join(FLORES_DIR, "jpn_Jpan.devtest"), encoding="utf-8") as fh:
        ja = [l.rstrip("\n") for l in fh]
    with open(os.path.join(FLORES_DIR, "ind_Latn.devtest"), encoding="utf-8") as fh:
        idn = [l.rstrip("\n") for l in fh]
    assert len(ja) == len(idn), f"{len(ja)} vs {len(idn)}"

    rng = random.Random(args.seed)
    idxs = sorted(rng.sample(range(len(ja)), args.n))

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "devtest.sample.ja.txt"), "w", encoding="utf-8") as f1, \
         open(os.path.join(OUT_DIR, "devtest.sample.ind.txt"), "w", encoding="utf-8") as f2, \
         open(os.path.join(OUT_DIR, "devtest.sample.idx.txt"), "w", encoding="utf-8") as f3:
        for i in idxs:
            f1.write(ja[i] + "\n")
            f2.write(idn[i] + "\n")
            f3.write(f"{i}\n")

    print(f"sampled {len(idxs)} devtest lines (seed {args.seed}) of {len(ja)} total")
    print(f"-> {OUT_DIR}/devtest.sample.[ja|ind|idx].txt")


if __name__ == "__main__":
    main()
