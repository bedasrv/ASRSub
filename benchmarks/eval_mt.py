#!/usr/bin/env python3
"""MT quality eval: translate FLORES-200 devtest sample (jpn_Jpan -> ind_Latn)
with the local HY-MT1.5-7B (OpenAI-compatible llama-server :8011), score with
sacrebleu spBLEU (flores200 tokenizer).

Caveat: the model tends to merge consecutive short lines into one output line,
so every source line is sent as a single-line request (slower but keeps the
1:1 line pairing needed for BLEU).

Usage:
  ~/benchmark/venvs/eval/bin/python benchmarks/eval_mt.py
"""

import argparse
import json
import os
import re
import time
import urllib.request

import sacrebleu

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(HERE, "data", "flores_sample")
RESULTS_DIR = os.path.join(HERE, "results")
BASE = "http://127.0.0.1:8011/v1"
MODEL = "HY-MT1.5-7B-Q4_K_M.gguf"
NUM_RETRIES = 4


def post_chat(line: str, timeout=240):
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system",
             "content": "Translate the following Japanese sentence into natural Indonesian. "
                        "Reply with only the translation, no extra text, no numbering."},
            {"role": "user", "content": line},
        ],
        "temperature": 0.1,
        "max_tokens": 512,
    }
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(NUM_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            text = data["choices"][0]["message"]["content"].strip()
            return text
        except Exception as e:
            wait = 5 * (attempt + 1)
            print(f"  retry {attempt + 1} after error {e!r}, sleeping {wait}s")
            time.sleep(wait)
    return None


def clean(text: str) -> str:
    text = re.sub(r"^\s*\d+[\.\:\)]\s*", "", text)
    text = re.sub(r"^>\s*", "", text)
    return text.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-lines", type=int, default=300)
    ap.add_argument("--out", default=os.path.join(RESULTS_DIR, "mt_hy_mt15_flores200.json"))
    args = ap.parse_args()

    with open(os.path.join(SAMPLE_DIR, "devtest.sample.ja.txt"), encoding="utf-8") as fh:
        src = [l.rstrip("\n") for l in fh]
    with open(os.path.join(SAMPLE_DIR, "devtest.sample.ind.txt"), encoding="utf-8") as fh:
        ref = [l.rstrip("\n") for l in fh]

    if args.max_lines:
        src = src[: args.max_lines]
        ref = ref[: args.max_lines]

    sys_out = []
    t0 = time.time()
    sys_path = os.path.join(RESULTS_DIR, "mt_hy_mt15_flores200.sys.txt")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    for i, line in enumerate(src):
        hyp = post_chat(line)
        if hyp is None:
            print(f"[{i + 1}/{len(src)}] FAILED, using empty hypothesis")
            hyp = ""
        sys_out.append(clean(hyp))
        with open(sys_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(sys_out) + "\n")
        if (i + 1) % 25 == 0:
            print(f"[{i + 1}/{len(src)}] elapsed {time.time() - t0:.0f}s")

    elapsed = time.time() - t0
    # spBLEU: flores200 tokenizer (SPM model pre-cached in ~/.sacrebleu/models/;
    # the tinyurl flores200 URL is dead, same model as flores101/sacrebleu_tokenizer_spm.model)
    bleu = sacrebleu.corpus_bleu(sys_out, [ref], tokenize="flores200")
    print("=" * 60)
    print(f"lines: {len(src)}  elapsed: {elapsed:.0f}s ({elapsed / len(src):.2f}s/line)")
    print(f"spBLEU (flores200 tok): {bleu.score:.2f}")
    print(f"  {bleu}")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({
            "task": "FLORES-200 devtest jpn_Jpan->ind_Latn",
            "model": MODEL,
            "num_lines": len(src),
            "spBLEU": bleu.score,
            "precisions": list(bleu.precisions),
            "bp": bleu.bp,
            "ratio": bleu.ratio,
            "elapsed_s": round(elapsed, 1),
            "hyps": sys_out,
        }, fh, ensure_ascii=False, indent=2)
    print(f"saved: {args.out}")


if __name__ == "__main__":
    main()
