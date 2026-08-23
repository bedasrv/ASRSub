#!/usr/bin/env python3
"""Temporary end-to-end smoke driver for orchestrator v2 (not a service).

Runs the full v2 chain on ONE video the way run_pass would: probe/choose
source -> ffmpeg extract -> faster-whisper+VAD -> segment split -> 7B
merge-aware translate (with glossary) -> write <base>.<lang>.srt with AI
marker as a REAL first cue -> validate (timing, CJK, empties, >8s spans,
name drift, marker cue).

Usage (on the PC):
  LD_LIBRARY_PATH=/usr/local/lib/ollama/cuda_v12 \
  /home/user/benchmark/venvs/fw/bin/python pipeline/smoke_v2.py \
    --video "<path>.mkv" --stream 0 --out /tmp/dxd_v2
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orchestrator as o
from pipeline import asr as pasr

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{1,2}):(\d{2}):(\d{2}),(\d{3})")
MUSIC_RE = re.compile(r"\(Lirik lagu\)|（歌詞）|\(Musik\)|\(SFX\)|\(Lagu\)|\(Musica\)")


def to_ms(h, m, s, ms):
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def parse_srt_file(path):
    cues = []
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        ts_idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ts_idx is None:
            continue
        m = TS_RE.search(lines[ts_idx])
        if not m:
            continue
        body = " ".join(lines[ts_idx + 1 :]).strip()
        cues.append(
            {
                "start": to_ms(*m.group(1, 2, 3, 4)),
                "end": to_ms(*m.group(5, 6, 7, 8)),
                "text": body,
            }
        )
    return cues


def validate(path, out_base, lang):
    issues = []
    first_line = open(path, encoding="utf-8").readline().strip()
    if first_line == o.AI_MARKER:
        issues.append("bare AI marker header line (must be a real cue)")
    if not first_line.isdigit():
        issues.append(f"first line is not a cue index: {first_line!r}")
    if not path.endswith(f".{lang}.srt"):
        issues.append(f"filename does not end .{lang}.srt: {path}")
    cues = parse_srt_file(path)
    if not cues:
        issues.append("no cues parsed")
        return issues, cues
    if cues[0]["text"] != o.AI_MARKER:
        issues.append(f"first cue text is not AI marker: {cues[0]['text']!r}")
    elif cues[0]["start"] != 0:
        issues.append(f"marker cue does not start at 0: {cues[0]['start']}")
    prev_end = -1
    timing_violations = 0
    empties = 0
    long_spans = []
    cjk_lines = []
    for c in cues:
        if c["start"] < prev_end or c["end"] < c["start"]:
            timing_violations += 1
            issues.append(f"timing violation {c['start']}->{c['end']}")
        prev_end = c["end"]
        if not c["text"].strip():
            empties += 1
        if c["end"] - c["start"] > 8000 and not MUSIC_RE.search(c["text"]):
            long_spans.append((c["start"], c["end"], c["text"][:60]))
        if CJK.search(c["text"]) and not MUSIC_RE.search(c["text"]):
            cjk_lines.append((c["start"], c["text"][:60]))
    if timing_violations:
        issues.append(f"timing violations: {timing_violations}")
    if cjk_lines:
        issues.append(f"CJK lines: {len(cjk_lines)} (first: {cjk_lines[:3]})")
    if empties > 2:
        issues.append(f"empty cues: {empties} (>2)")
    if long_spans:
        issues.append(f"cues >8s: {len(long_spans)} (first: {long_spans[:3]})")
    all_text = " ".join(c["text"] for c in cues)
    drift = [n for n in ("Issai", "Arshia", "Azazal", "Lizevim") if n in all_text]
    if drift:
        issues.append(f"name drift detected: {drift}")
    for good in ("Issei", "Asia", "Azazel"):
        if good not in all_text:
            issues.append(f"expected name not found in output: {good}")
    print(f"  cues: {len(cues)}")
    print(f"  timing violations: {timing_violations}")
    print(f"  empty cues: {empties}")
    print(f"  CJK lines: {len(cjk_lines)}")
    print(f"  cues >8s: {len(long_spans)}")
    print(f"  first cue: {cues[0]['start']}->{cues[0]['end']} {cues[0]['text']}")
    print(f"  file: {path}")
    return issues, cues


def main(argv):
    ap = argparse.ArgumentParser(description="orchestrator v2 end-to-end smoke")
    ap.add_argument("--video", required=True)
    ap.add_argument("--stream", type=int, default=0)
    ap.add_argument("--lang", default="id", choices=["id", "en"])
    ap.add_argument("--series", default=None)
    ap.add_argument("--out", default="/tmp/dxd_v2")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.video):
        print(f"ERROR: video not found: {args.video}", file=sys.stderr)
        return 1

    out_path = f"{args.out}.{args.lang}.srt"
    wav = f"/tmp/smoke_v2_{os.getpid()}.wav"
    t0 = time.time()
    try:
        streams = o.probe_audio(args.video)
        decision = o.choose_source(streams, args.lang)
        print(f"probe: {len(streams)} audio streams; decision: {decision}")
        if decision is None:
            print("ERROR: no audio streams", file=sys.stderr)
            return 1
        sel = next(s for s in streams if s["index"] == decision["stream_index"])
        o.extract_wav(args.video, sel["index"], wav)
        print(f"extract: {wav} ({time.time() - t0:.0f}s)")

        t_asr = time.time()
        cues = pasr.transcribe_cues(wav, language=decision["asr_lang"])
        cues = [c for c in cues if c["text"].strip()]
        asr_s = time.time() - t_asr
        print(f"ASR: {asr_s:.0f}s, {len(cues)} cues")

        texts = [c["text"] for c in cues]
        cfg = {
            "TRANSLATE_BASE": "http://127.0.0.1:8011/v1",
            "TRANSLATE_MODEL": "/home/user/Documents/Tools/llama-cpp-turboquant/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf",
            "SDH_PLACEHOLDERS": list(o.DEFAULT_SDH_PLACEHOLDERS),
        }
        key = "smoke"
        groups = o.translate_texts(
            cfg,
            cues,
            args.lang,
            key,
            series_title=args.series,
        )
        if groups is None:
            print("ERROR: translation returned None", file=sys.stderr)
            return 1
        cues_out = []
        texts_out = []
        for s, e, t in groups:
            cues_out.append({"start": cues[s]["start"], "end": cues[e - 1]["end"]})
            texts_out.append(t)
        o.write_srt(cues_out, texts_out, out_path, header=o.AI_MARKER)
        print(f"translate+write: {time.time() - t0:.0f}s total, {len(texts_out)} lines")

        issues, _cues = validate(out_path, args.out, args.lang)
        print(f"elapsed_s: {round(time.time() - t0, 1)}")
        if issues:
            print("SMOKE FAIL:")
            for i in issues:
                print(f"  - {i}")
            return 1
        print("SMOKE PASS")
        return 0
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
