#!/usr/bin/env python3
"""Prepare a deterministic 100-200 utterance subset of ReazonSpeech for ASR eval.

Source: japanese-asr/ja_asr.reazonspeech_test (HuggingFace, not gated) -- the
official ReazonSpeech held-out test split used by kotoba-whisper (5263 utt,
16 kHz wav bytes in parquet). We sample N utterances (default 150) with fixed
seed, cap per-utterance duration, and cap total duration at 2h.
Output: manifest.jsonl + decoded WAVs.

Uses pyarrow + soundfile directly (datasets 5.x audio decoding would require
torchcodec).
"""

import argparse
import glob
import io
import json
import os
import random

import pyarrow.parquet as pq
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data", "reazonspeech_test")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet-dir", required=True, help="dir containing test-*.parquet")
    ap.add_argument("--n", type=int, default=150, help="target utterance count")
    ap.add_argument("--max-utt-s", type=float, default=45.0)
    ap.add_argument("--max-total-s", type=float, default=7200.0)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.parquet_dir, "test-*.parquet")))
    if not files:
        raise SystemExit(f"no parquet files under {args.parquet_dir}")

    texts = []
    audios = []
    for f in files:
        pf = pq.ParquetFile(f)
        for rg in range(pf.num_row_groups):
            t = pf.read_row_group(rg)
            texts.extend(t.column("transcription").to_pylist())
            audios.extend(t.column("audio").combine_chunks().field("bytes").to_pylist())
    assert len(texts) == len(audios)
    print(f"loaded {len(texts)} utterances")

    rng = random.Random(args.seed)
    idxs = list(range(len(texts)))
    rng.shuffle(idxs)

    os.makedirs(os.path.join(DATA_DIR, "wav"), exist_ok=True)
    manifest = []
    total_s = 0.0
    for i in idxs:
        if len(manifest) >= args.n:
            break
        if total_s >= args.max_total_s:
            break
        try:
            y, sr = sf.read(io.BytesIO(audios[i]))
        except Exception as e:
            print(f"  skip utt {i}: audio decode failed: {e!r}")
            continue
        if sr != 16000:
            print(f"  skip utt {i}: sr={sr} (expected 16000)")
            continue
        dur = len(y) / sr
        if dur > args.max_utt_s:
            continue
        if total_s + dur > args.max_total_s:
            continue
        wav = os.path.join(DATA_DIR, "wav", f"utt_{i:05d}.wav")
        sf.write(wav, y, 16000)
        manifest.append(
            {"id": i, "wav": wav, "text": texts[i], "duration_s": round(dur, 3)}
        )
        total_s += dur

    with open(os.path.join(DATA_DIR, "manifest.jsonl"), "w", encoding="utf-8") as fh:
        for m in manifest:
            fh.write(json.dumps(m, ensure_ascii=False) + "\n")

    print(f"selected {len(manifest)} utterances, total {total_s:.1f}s "
          f"({total_s / 3600:.2f}h), mean {total_s / len(manifest):.1f}s/utt")
    print(f"manifest: {os.path.join(DATA_DIR, 'manifest.jsonl')}")


if __name__ == "__main__":
    main()
