#!/usr/bin/env python3
"""Local NLLB-200-3.3B (CT2 int8) baseline on the FLORES-200 sample
(jpn_Jpan -> ind_Latn), scored with sacrebleu spBLEU.

Model: OpenNMT/nllb-200-3.3B-ct2-int8 (ctranslate2 int8, ~3.2GB).
Tokenizer: transformers AutoTokenizer (same repo).
Usage: ~/benchmark/venvs/eval/bin/python benchmarks/eval_nllb.py [--cpu]
"""

import argparse
import json
import os
import time

import ctranslate2
import sacrebleu
from transformers import AutoTokenizer

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(HERE, "data", "flores_sample")
RESULTS_DIR = os.path.join(HERE, "results")
MODEL_ID = "OpenNMT/nllb-200-3.3B-ct2-int8"
SRC_LANG = "jpn_Jpan"
TGT_LANG = "ind_Latn"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-lines", type=int, default=300)
    ap.add_argument("--beam", type=int, default=4)
    ap.add_argument("--cpu", action="store_true")
    args = ap.parse_args()

    from huggingface_hub import snapshot_download

    t0 = time.time()
    model_path = snapshot_download(MODEL_ID)
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    translator = ctranslate2.Translator(
        model_path, device="cpu" if args.cpu else "cuda", compute_type="int8",
    )
    print(f"model load: {time.time() - t0:.0f}s (device: {'cpu' if args.cpu else 'cuda'})")

    with open(os.path.join(SAMPLE_DIR, "devtest.sample.ja.txt"), encoding="utf-8") as fh:
        src = [l.rstrip("\n") for l in fh]
    with open(os.path.join(SAMPLE_DIR, "devtest.sample.ind.txt"), encoding="utf-8") as fh:
        ref = [l.rstrip("\n") for l in fh]
    if args.max_lines:
        src = src[: args.max_lines]
        ref = ref[: args.max_lines]

    t0 = time.time()
    hyps = []
    for i, line in enumerate(src):
        src_tokens = [SRC_LANG] + tokenizer.convert_ids_to_tokens(
            tokenizer.encode(line, add_special_tokens=False)
        )
        results = translator.translate_batch(
            [src_tokens], target_prefix=[[TGT_LANG]], beam_size=args.beam, max_batch_size=16,
        )
        h = results[0].hypotheses[0]
        hyps.append(tokenizer.decode(tokenizer.convert_tokens_to_ids(h[1:])).strip())
        if (i + 1) % 50 == 0:
            print(f"[{i + 1}/{len(src)}] elapsed {time.time() - t0:.0f}s", flush=True)
    elapsed = time.time() - t0

    bleu = sacrebleu.corpus_bleu(hyps, [ref], tokenize="flores200")
    print("=" * 60)
    print(f"lines: {len(src)}  elapsed: {elapsed:.0f}s ({elapsed / len(src):.2f}s/line)")
    print(f"spBLEU (flores200 tok): {bleu.score:.2f}")
    print(f"  {bleu}")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "nllb300.sys.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(hyps) + "\n")
    with open(os.path.join(RESULTS_DIR, "mt_nllb200_3b3_flores300.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "task": "FLORES-200 devtest jpn_Jpan->ind_Latn",
            "model": MODEL_ID,
            "device": "cpu" if args.cpu else "cuda",
            "beam": args.beam,
            "num_lines": len(src),
            "spBLEU": bleu.score,
            "precisions": list(bleu.precisions),
            "bp": bleu.bp,
            "ratio": bleu.ratio,
            "elapsed_s": round(elapsed, 1),
        }, fh, ensure_ascii=False, indent=2)
    print(f"saved: {os.path.join(RESULTS_DIR, 'mt_nllb200_3b3_flores300.json')}")


if __name__ == "__main__":
    main()
