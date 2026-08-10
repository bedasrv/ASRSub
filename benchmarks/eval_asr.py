#!/usr/bin/env python3
"""ASR fidelity eval: faster-whisper large-v3-turbo (int8, CUDA) + Silero VAD on
a ReazonSpeech subset. Reports CER and character-based WER (Japanese-appropriate
tokenization: characters as tokens) via jiwer.

Run from the faster-whisper venv with CUDA12 libs:
  LD_LIBRARY_PATH=/usr/local/lib/ollama/cuda_v12 \
    ~/benchmark/venvs/fw/bin/python benchmarks/eval_asr.py \
    --manifest benchmarks/data/reazonspeech_test/manifest.jsonl
"""

import argparse
import json
import os
import re
import time
import unicodedata

import jiwer

CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
PUNC_RE = re.compile(r"[、。，．！？「」『』（）()…・\s]+")


def jp_normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = PUNC_RE.sub("", t)
    return t.strip()


def char_split(text: str) -> str:
    return " ".join(list(text))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute", default="int8")
    ap.add_argument("--name", default="reazonspeech_test",
                    help="dataset name for the results file")
    args = ap.parse_args()

    import faster_whisper
    from faster_whisper.vad import VadOptions

    t0_all = time.time()
    print(f"faster-whisper {faster_whisper.__version__}")

    t0 = time.time()
    model = faster_whisper.WhisperModel(args.model, device=args.device, compute_type=args.compute)
    print(f"model load: {time.time() - t0:.1f}s")

    with open(args.manifest, encoding="utf-8") as fh:
        rows = [json.loads(l) for l in fh]

    hyps = []
    refs = []
    per_utt = []
    for r in rows:
        t0 = time.time()
        segments, info = model.transcribe(
            r["wav"],
            language="ja",
            vad_filter=True,
            vad_parameters=VadOptions(min_silence_duration_ms=500, max_speech_duration_s=8),
            condition_on_previous_text=False,
            beam_size=5,
            initial_prompt="こんにちは。これはアニメの台詞です。",
        )
        hyp = "".join((s.text or "") for s in segments)
        hyps.append(hyp)
        refs.append(r["text"])
        elapsed = time.time() - t0
        per_utt.append({"id": r["id"], "ref": r["text"], "hyp": hyp,
                        "duration_s": r["duration_s"], "rtf": elapsed / r["duration_s"]})
        print(f"[{len(hyps)}/{len(rows)}] utt {r['id']} {r['duration_s']:6.1f}s "
              f"rtf={elapsed / r['duration_s']:.2f}")

    n = len(rows)
    refs_n = [jp_normalize(x) for x in refs]
    hyps_n = [jp_normalize(x) for x in hyps]

    cer = jiwer.cer(refs_n, hyps_n)
    wer_char = jiwer.wer([char_split(x) for x in refs_n], [char_split(x) for x in hyps_n])
    wer_word_naive = jiwer.wer(refs_n, hyps_n)

    total_audio = sum(r["duration_s"] for r in rows)
    total_wall = time.time() - t0_all

    results = {
        "dataset": args.name,
        "model": args.model,
        "compute": args.compute,
        "num_utterances": n,
        "total_audio_s": round(total_audio, 1),
        "cer": round(cer, 4),
        "wer_char": round(wer_char, 4),
        "wer_word_naive_informational": round(wer_word_naive, 4),
        "mean_rtf": round(sum(p["rtf"] for p in per_utt) / n, 3),
        "total_wall_s": round(total_wall, 1),
        "per_utterance": per_utt,
    }
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results",
                       f"asr_{args.name}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)

    print("=" * 60)
    print(f"utterances: {n}  total audio: {total_audio:.0f}s ({total_audio/3600:.2f}h)")
    print(f"CER (char edit distance):              {cer * 100:.2f}%")
    print(f"WER (char-based tokenization):         {wer_char * 100:.2f}%")
    print(f"WER (naive English-style, info only):  {wer_word_naive * 100:.2f}%")
    print(f"mean RTF: {results['mean_rtf']:.3f}  wall: {total_wall:.0f}s")
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
