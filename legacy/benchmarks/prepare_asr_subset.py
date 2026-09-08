#!/usr/bin/env python3
"""Prepare a deterministic 100-200 utterance subset of a ja_asr.* HF dataset
for ASR eval (audio bytes + transcription in parquet).

Supports any of the japanese-asr/ja_asr.* mirrors:
  - reazonspeech_test (16 kHz)   -> ~5263 test utterances
  - jsut_basic5000 (16 kHz)      -> ~5000 clean read utterances
  - common_voice_8_0 (48 kHz)    -> ~4483 noisy crowd utterances (resampled)

Samples N utterances with fixed seed, caps per-utterance duration and total
duration (default 2h). Output: manifest.jsonl + decoded WAVs (16 kHz mono).
Uses pyarrow + soundfile (+ scipy for resampling) directly -- datasets 5.x
audio decoding would require torchcodec.
"""

import argparse
import glob
import io
import json
import os
import random

import numpy as np
import pyarrow.parquet as pq
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))


def resample(y, src_sr, dst_sr=16000):
    from scipy.signal import resample_poly

    gcd = np.gcd(src_sr, dst_sr)
    return resample_poly(y, dst_sr // gcd, src_sr // gcd).astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="dataset name, e.g. reazonspeech_test")
    ap.add_argument("--parquet-dir", required=True, help="dir containing *.parquet")
    ap.add_argument("--n", type=int, default=200, help="target utterance count")
    ap.add_argument("--max-utt-s", type=float, default=45.0)
    ap.add_argument("--max-total-s", type=float, default=7200.0)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.parquet_dir, "*.parquet")))
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
    assert len(texts) == len(audios), f"{len(texts)} vs {len(audios)}"
    print(f"loaded {len(texts)} utterances from {len(files)} parquet files")

    rng = random.Random(args.seed)
    idxs = list(range(len(texts)))
    rng.shuffle(idxs)

    data_dir = os.path.join(HERE, "data", args.name)
    wav_dir = os.path.join(data_dir, "wav")
    os.makedirs(wav_dir, exist_ok=True)

    manifest = []
    total_s = 0.0
    skipped_sr = 0
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
        if sr == 48000:
            y = resample(y, 48000)
            sr = 16000
        if sr != 16000:
            skipped_sr += 1
            continue
        dur = len(y) / sr
        if dur > args.max_utt_s:
            continue
        if total_s + dur > args.max_total_s:
            continue
        wav = os.path.join(wav_dir, f"utt_{i:05d}.wav")
        sf.write(wav, y, 16000)
        manifest.append(
            {"id": i, "wav": wav, "text": texts[i], "duration_s": round(dur, 3)}
        )
        total_s += dur

    with open(os.path.join(data_dir, "manifest.jsonl"), "w", encoding="utf-8") as fh:
        for m in manifest:
            fh.write(json.dumps(m, ensure_ascii=False) + "\n")

    print(f"selected {len(manifest)} utterances, total {total_s:.1f}s "
          f"({total_s / 3600:.2f}h), mean {total_s / len(manifest):.1f}s/utt"
          + (f", skipped non-16k: {skipped_sr}" if skipped_sr else ""))
    print(f"manifest: {os.path.join(data_dir, 'manifest.jsonl')}")


if __name__ == "__main__":
    main()
