"""SenseVoice / FunASR fsmn-vad ASR backend for orchestrator v2.

Alternative to faster-whisper (orchestrator ASR_BACKEND=sensevoice). Uses the
VAD recipe empirically validated on the PC 2026-08-10 (Frieren S02E01 180s
slice, funasr 1.4.0, sensevoice venv; former monster spans 17.5-21s -> max
4.68s):

  vad_kwargs={"max_single_segment_time": 5000, "max_end_silence_time": 400,
              "speech_pad_ms": 150, "min_speech_duration_ms": 250}

vad_kwargs MUST be passed at AutoModel() CONSTRUCTION. Calling
model.inference(kwargs=...) with the kwargs unset at construction crashes
(chunk_size=None -> frontend.fs is NoneType); raw kwargs to inference() are
never used.

Pipeline (the working path, adapter-verified): generate(..., output_timestamp=
True, merge_vad=False) -> per-utterance marker-chunks with char timestamps ->
bucket chunks into fsmn-vad speech windows (best overlap) -> per-window cue ->
_min_dur_postpass: no cue < 1s is ever emitted (extend to 1s when the gap
allows, else merge with the closer neighbor — tie prefers the previous —
capped at 6s total; last resort drops the fragment). This replaces the doc's
200ms rule and _merge_tiny's 0.6s-gap rule for this path: with 400ms
max_end_silence_time, inter-utterance gaps of 300-900ms produce 41% sub-1s
fragments that neither rule resolves.

Cues are returned as {start_ms, end_ms, text} like pipeline/asr.py.
"""

import contextlib
import io
import os
import re
import threading

SV_MODEL_ID = os.environ.get("SV_MODEL_ID", "FunAudioLLM/SenseVoiceSmall")
SV_MAX_SEG_MS = int(os.environ.get("SV_MAX_SEG_MS", "5000"))
SV_MAX_END_SILENCE_MS = int(os.environ.get("SV_MAX_END_SILENCE_MS", "400"))
SV_SPEECH_PAD_MS = int(os.environ.get("SV_SPEECH_PAD_MS", "150"))
SV_MIN_SPEECH_MS = int(os.environ.get("SV_MIN_SPEECH_MS", "250"))
SV_MIN_CUE_MS = int(os.environ.get("SV_MIN_CUE_MS", "1000"))
SV_MAX_CUE_MS = int(os.environ.get("SV_MAX_CUE_MS", "6000"))
SV_MIN_GAP_MS = int(os.environ.get("SV_MIN_GAP_MS", "150"))

VAD_KWARGS = {
    "max_single_segment_time": SV_MAX_SEG_MS,
    "max_end_silence_time": SV_MAX_END_SILENCE_MS,
    "speech_pad_ms": SV_SPEECH_PAD_MS,
    "min_speech_duration_ms": SV_MIN_SPEECH_MS,
}

_MARKER = re.compile(r"<\|([^|]+)\|>")

_model = None
_model_lock = threading.Lock()


def get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from funasr import AutoModel

            with contextlib.redirect_stdout(io.StringIO()):
                _model = AutoModel(
                    model=SV_MODEL_ID,
                    vad_model="fsmn-vad",
                    vad_kwargs=VAD_KWARGS,
                    device="cuda:0",
                    hub="hf",
                    disable_update=True,
                )
    return _model


def reset_model():
    global _model
    with _model_lock:
        _model = None


def _parse_chunks(raw, timestamps):
    """Split raw text into (start_ms, end_ms, text) char-timestamp chunks.
    Event tags (<|Speech|>, <|BGM|>, <|EMO_*|>) are markers, not text."""
    parts = _MARKER.split(raw)
    char_offset = 0
    chunks = []
    for i in range(len(parts)):
        if i % 2 == 1:
            continue
        chunk = parts[i]
        if not chunk.strip():
            continue
        clean = "".join(chunk.split())
        start = char_offset
        end = char_offset + len(clean)
        char_offset = end
        if not timestamps:
            continue
        s_idx = min(start, len(timestamps) - 1)
        e_idx = min(max(end - 1, 0), len(timestamps) - 1)
        t0 = int(timestamps[s_idx][0])
        t1 = int(timestamps[e_idx][1])
        chunks.append((max(t0, 0), max(t1, t0), clean))
    return chunks


def _vad_segments_ms(wav_path):
    model = get_model()
    res = model.inference(
        input=wav_path,
        model=model.vad_model,
        kwargs=model.vad_kwargs,
        disable_pbar=True,
    )
    if not res:
        return []
    return [(int(seg[0]), int(seg[1])) for seg in res[0]["value"]]


def _overlap_ms(a_s, a_e, b_s, b_e):
    return max(0, min(a_e, b_e) - max(a_s, b_s))


def _min_dur_postpass(cues, min_dur_ms=SV_MIN_CUE_MS, max_dur_ms=SV_MAX_CUE_MS,
                      min_gap_ms=SV_MIN_GAP_MS):
    """Resolve every cue shorter than min_dur_ms. Never emits a <min_dur cue.

    1) extend the cue's end to min_dur_ms when the gap to the next cue allows
       (next.start - new_end >= min_gap_ms); a trailing cue extends freely;
    2) else merge with the NEIGHBOR — prefer the closer one, ties prefer the
       previous — capped at max_dur_ms total span;
    3) else drop the fragment (last resort; only reachable when both
       neighbors are near max_dur with tight gaps — prevents <1s garbage).
    Iterates until no short cue remains. Returns new cue dicts."""
    out = [dict(c) for c in cues]

    def merge_into(short_i, nb_i):
        cur, nb = out[short_i], out[nb_i]
        left, right = (cur, nb) if cur["start"] <= nb["start"] else (nb, cur)
        text = (left["text"].strip() + " " + right["text"].strip()).strip()
        out[min(short_i, nb_i)] = {
            "start": left["start"],
            "end": right["end"],
            "text": text,
        }
        del out[max(short_i, nb_i)]

    while True:
        i = next(
            (k for k, c in enumerate(out) if c["end"] - c["start"] < min_dur_ms),
            None,
        )
        if i is None:
            return out
        cur = out[i]
        prev = out[i - 1] if i > 0 else None
        nxt = out[i + 1] if i + 1 < len(out) else None
        if nxt is None:
            cur["end"] = cur["start"] + min_dur_ms
            continue
        if cur["start"] + min_dur_ms <= nxt["start"] - min_gap_ms:
            cur["end"] = cur["start"] + min_dur_ms
            continue
        g_prev = (cur["start"] - prev["end"]) if prev else float("inf")
        g_next = (nxt["start"] - cur["end"]) if nxt else float("inf")
        cands = []
        if prev is not None:
            cands.append((i - 1, g_prev))
        if nxt is not None:
            cands.append((i + 1, g_next))
        cands.sort(key=lambda x: (x[1], x[0]))
        merged = False
        for nb_i, _gap in cands:
            nb = out[nb_i]
            span = max(nb["end"], cur["end"]) - min(nb["start"], cur["start"])
            if span <= max_dur_ms:
                merge_into(i, nb_i)
                merged = True
                break
        if merged:
            continue
        new_end = min(cur["start"] + min_dur_ms, nxt["start"] - min_gap_ms)
        if new_end > cur["end"]:
            cur["end"] = new_end
        else:
            del out[i]


def transcribe_cues(wav_path, language="ja"):
    """SenseVoice + fsmn-vad transcription; 5s VAD cap + min-1s post-pass.
    Returns {start_ms, end_ms, text} cues (no cue < 1s; spans <= 6s)."""
    model = get_model()
    results = model.generate(
        input=wav_path,
        cache={},
        language=language,
        use_itn=True,
        batch_size_s=60,
        merge_vad=False,
        output_timestamp=True,
        disable_pbar=True,
    )

    all_chunks = []
    for res in results:
        raw = res.get("text", "")
        timestamps = res.get("timestamp") or []
        all_chunks.extend(_parse_chunks(raw, timestamps))
    if not all_chunks:
        return []

    vad = _vad_segments_ms(wav_path)

    buckets = {}
    for chunk in all_chunks:
        best, best_ov = -1, 0.0
        for vi, (vs, ve) in enumerate(vad):
            ov = _overlap_ms(chunk[0], chunk[1], vs, ve)
            if ov > best_ov:
                best_ov, best = ov, vi
        buckets.setdefault(best if best >= 0 else len(vad) + len(buckets), []).append(chunk)

    cues = []
    for vi in sorted(buckets):
        chunks = sorted(buckets[vi], key=lambda c: c[0])
        if not chunks:
            continue
        text = " ".join(c[2] for c in chunks).strip()
        if text:
            cues.append(
                {
                    "start": chunks[0][0],
                    "end": max(c[1] for c in chunks),
                    "text": text,
                }
            )
    cues.sort(key=lambda c: c["start"])
    return _min_dur_postpass(cues)