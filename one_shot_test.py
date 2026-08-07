#!/usr/bin/env python3
"""Single-video subtitle quality test: faster-whisper ASR -> HY-MT1.5 translate -> SRT."""

import argparse
import os
import re
import subprocess
import sys
import time

import orchestrator as o


def ffprobe_audio(video):
    p = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index,codec_name,channels",
            "-of",
            "json",
            video,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {p.stderr}")
    import json

    return json.loads(p.stdout).get("streams", [])


def extract_audio(video, stream_index, out_wav):
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            video,
            "-map",
            f"0:{stream_index}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            out_wav,
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )


def transcribe(model, wav):
    segments, _info = model.transcribe(
        wav,
        language="ja",
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        condition_on_previous_text=False,
        beam_size=5,
        initial_prompt="こんにちは。これはアニメの台詞です。",
    )
    return segments


CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
TRANSLATE_CHUNK = 10


def _local_chunk(cfg, chunk, lang_name, key):
    """One local-model chat call for a numbered chunk. Returns parsed dict num->text
    (via orchestrator._parse_numbered_response) or None on network failure."""
    n = len(chunk)
    system = (
        f"Translate each line into {lang_name}. Reply as numbered list, "
        f"e.g. 1. ... 2. ... 3. ..., exactly {n} lines, no extra text."
    )
    prompt = system + "\n\n" + "\n".join(f"{i}. {l}" for i, l in enumerate(chunk, 1))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    raw = o.post_chat(cfg, messages, cfg["TRANSLATE_MODEL"], key, local=True)
    if raw is None:
        return None
    return o._parse_numbered_response(raw)


def _attempt_chunk(cfg, chunk, lang_name, key, echo_probe=11):
    """Up to 3 attempts per chunk; corrective retry on echo. Returns clean parsed
    dict or None (all attempts empty/echo/network-fail)."""
    n = len(chunk)
    for _ in range(3):
        parsed = _local_chunk(cfg, chunk, lang_name, key)
        if not parsed:
            continue
        echo = False
        for k in range(1, min(echo_probe, n) + 1):
            t = parsed.get(k)
            if t and CJK_RE.search(t):
                echo = True
                break
        if not echo:
            return parsed
    return None


def _complete_tail(cfg, tail_lines, m, lang_name, key):
    """Complete a chunk's missing tail: translate source lines m..n-1 (numbered
    m+1..n in the prompt), remap keys to absolute m+i. Returns merged dict for the
    full chunk (keys 1..n) or None after 3 failed attempts."""
    n = m + len(tail_lines)
    for _ in range(3):
        system = (
            f"Translate each line into {lang_name}. Reply as numbered list, "
            f"e.g. 1. ... 2. ... 3. ..., exactly {n - m} lines, no extra text."
        )
        prompt = (
            system
            + "\n\n"
            + "\n".join(f"{i + 1}. {l}" for i, l in enumerate(tail_lines))
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        raw = o.post_chat(cfg, messages, cfg["TRANSLATE_MODEL"], key, local=True)
        if raw is None:
            continue
        parsed = o._parse_numbered_response(raw)
        if not parsed:
            continue
        echo = False
        for rel in range(1, min(5, n - m) + 1):
            t = parsed.get(rel)
            if t and CJK_RE.search(t):
                echo = True
                break
        if echo:
            continue
        remapped = {m + i: text for i, text in parsed.items()}
        return remapped
    return None


def _translate_merge_aware(cfg, lines, lang_name, key):
    """Translate, tolerating HY-MT merging consecutive short lines. Returns list of
    (cue_start_idx, cue_end_idx_exclusive, text)."""
    groups = []
    cs = TRANSLATE_CHUNK
    for start in range(0, len(lines), cs):
        chunk = lines[start : start + cs]
        n = len(chunk)
        parsed = _attempt_chunk(cfg, chunk, lang_name, key)
        if parsed is None:
            for j in range(n):
                gi = start + j
                p = _attempt_chunk(cfg, [chunk[j]], lang_name, key, echo_probe=1)
                text = re.sub(r"^>\s*", "", (p or {}).get(1, ""))
                groups.append((gi, gi + 1, text))
            continue
        entries = sorted(parsed.items())
        m = len(entries)
        if m == n:
            for k, t in entries:
                groups.append((start + k - 1, start + k, re.sub(r"^>\s*", "", t)))
            continue
        entries_dict = dict(entries)
        tail_lines = chunk[m:n]
        merged = None
        if tail_lines:
            completion = _complete_tail(cfg, tail_lines, m, lang_name, key)
            if completion:
                merged = {**entries_dict, **completion}
        if merged:
            for k, t in sorted(merged.items()):
                groups.append((start + k - 1, start + k, re.sub(r"^>\s*", "", t)))
        else:
            for k, t in entries:
                gi = start + k - 1
                groups.append((gi, gi + 1, re.sub(r"^>\s*", "", t)))
            for k in range(m + 1, n + 1):
                gi = start + k - 1
                p = _attempt_chunk(cfg, [chunk[k - 1]], lang_name, key, echo_probe=1)
                text = re.sub(r"^>\s*", "", (p or {}).get(1, ""))
                groups.append((gi, gi + 1, text))
    return groups


def main(argv):
    ap = argparse.ArgumentParser(description="Single-video subtitle quality test")
    ap.add_argument("--video", required=True, help="input video path")
    ap.add_argument("--lang", default="id", choices=["id", "en"])
    ap.add_argument("--out", default=None, help="output SRT path")
    ap.add_argument(
        "--stream",
        type=int,
        default=1,
        help="audio stream index (0-based within audio streams)",
    )
    ap.add_argument("--whisper", default="large-v3-turbo", help="faster-whisper model")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute", default="int8")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.video):
        print(f"ERROR: video not found: {args.video}", file=sys.stderr)
        return 1

    streams = ffprobe_audio(args.video)
    if not streams:
        print(f"ERROR: no audio streams in {args.video}", file=sys.stderr)
        return 1
    if args.stream >= len(streams):
        avail = ", ".join(
            f"[{i}] {s.get('codec_name')} ch={s.get('channels')}"
            for i, s in enumerate(streams)
        )
        print(
            f"ERROR: stream {args.stream} out of range; available audio streams: {avail}",
            file=sys.stderr,
        )
        return 1

    out = args.out
    if not out:
        d = os.path.dirname(args.video)
        base = os.path.splitext(os.path.basename(args.video))[0]
        out = os.path.join(d, f"{base}.test.{args.lang}.srt")

    wav = f"/tmp/oneshot_{os.getpid()}.wav"
    t0 = time.time()
    try:
        sel = streams[args.stream]
        extract_audio(args.video, sel["index"], wav)

        from faster_whisper import WhisperModel

        model = WhisperModel(
            args.whisper, device=args.device, compute_type=args.compute
        )
        segments = transcribe(model, wav)

        cues = []
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            cues.append(
                {
                    "start": int(seg.start * 1000),
                    "end": int(seg.end * 1000),
                    "text": text,
                }
            )

        texts = [c["text"] for c in cues]
        sanitized = o.sanitize_lines(texts)
        guarded, _foreign = o.guard_foreign_lines(sanitized)
        cfg = {
            "TRANSLATE_BASE": "http://127.0.0.1:8011/v1",
            "TRANSLATE_MODEL": "HY-MT1.5-7B-Q4_K_M.gguf",
        }
        lang_name = o.LANG_NAMES.get(args.lang, args.lang)
        groups = _translate_merge_aware(cfg, guarded, lang_name, "oneshot")
        cues_out = []
        texts_out = []
        for s, e, t in groups:
            cues_out.append({"start": cues[s]["start"], "end": cues[e - 1]["end"]})
            texts_out.append(t)
        o.write_srt(cues_out, texts_out, out)

        num_chunks = (len(guarded) + TRANSLATE_CHUNK - 1) // TRANSLATE_CHUNK
        total_chars = sum(len(t) for t in texts_out)
        elapsed_s = round(time.time() - t0, 1)

        cjk_re = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
        srt_cjk = 0
        with open(out, encoding="utf-8") as fh:
            for line in fh:
                if (
                    cjk_re.search(line)
                    and "-->" not in line
                    and not line.strip().isdigit()
                ):
                    srt_cjk += 1

        print(f"num_cues: {len(cues_out)}")
        print(f"num_chunks_used: {num_chunks}")
        print(f"total_chars: {total_chars}")
        print(f"elapsed_s: {elapsed_s}")
        print(f"output_cjk_lines: {srt_cjk}")
        print(f"srt: {out}")
        return 0
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
