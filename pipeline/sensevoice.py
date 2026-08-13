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
bucket chunks into fsmn-vad speech windows (best overlap) -> split each window
at sentence boundaries -> one cue per sentence with REAL per-chunk timestamps
(start = first chunk's start_ms, end = final chunk's end_ms) ->
_min_dur_postpass: no cue < 1s is ever emitted (extend to 1s into adjacent
silence — forward into the gap after the cue, else backward into the gap
before it, so short sentences keep their own cue — else merge with the closer
neighbor, tie prefers the previous, capped at 6s total; last resort drops the
fragment). This replaces the doc's
200ms rule and _merge_tiny's 0.6s-gap rule for this path: with 400ms
max_end_silence_time, inter-utterance gaps of 300-900ms produce 41% sub-1s
fragments that neither rule resolves.

Sentence-boundary split: walking a window's sorted chunks, the current cue is
closed when a chunk's text ends with one of SENTENCE_END_PUNCT (env override,
default "。！？…") or when the gap to the next chunk is >= SV_MIN_GAP_MS. This
keeps multi-sentence walls of text (22% of cues held 2+ sentences) out of the
ASR output; _min_dur_postpass then merges short sentence fragments instead of
sentence stacks. Commas (、,) are never split points.

VAD-window clamping: each bucket keeps the (vs, ve) bounds of the fsmn-vad
window it belongs to, and every cue assembled from it is clamped into those
bounds. SenseVoice's char-level timestamp array can spill an utterance's tail
chars into the next speech onset (56s monster spans for ~600ms of speech);
the VAD windows are correct, so clamping restores real cue spans. Synthetic
fallback buckets (no VAD overlap) are unclamped. _min_dur_postpass runs after
clamping and may extend a clamped sub-1s cue into adjacent silence.

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
SENTENCE_END_PUNCT = os.environ.get("SENTENCE_END_PUNCT", "。！？…")

_SENT_END_RE = re.compile("[" + re.escape(SENTENCE_END_PUNCT) + "]$")

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


def _parse_chunks(raw, timestamps, capture_bgm=False):
    """Split raw text into (start_ms, end_ms, text) char-timestamp chunks.
    Event tags (<|Speech|>, <|BGM|>, <|EMO_*|>) are markers, not text.
    capture_bgm=True additionally returns a parallel list of bools marking
    chunks that follow a <|BGM|> marker (the marker is stripped as usual)."""
    parts = _MARKER.split(raw)
    char_offset = 0
    chunks = []
    bgm = []
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
        bgm.append(i > 0 and parts[i - 1] == "BGM")
    if capture_bgm:
        return chunks, bgm
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

    1) extend the cue to min_dur_ms using adjacent silence — forward into the
       gap after it first (next.start - new_end >= min_gap_ms), then backward
       into the gap before it (new_start - prev.end >= min_gap_ms) so a short
       sentence keeps its own cue instead of re-joining its neighbor; a
       trailing cue extends freely;
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
        if prev is not None:
            new_start = cur["end"] - min_dur_ms
            if new_start >= prev["end"] + min_gap_ms:
                cur["start"] = new_start
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


def _assemble_cue(chunks):
    """One cue from consecutive (start_ms, end_ms, text) chunks: REAL
    timestamps — start = first chunk's start, end = final chunk's end."""
    return {
        "start": chunks[0][0],
        "end": chunks[-1][1],
        "text": " ".join(c[2] for c in chunks).strip(),
    }


def _split_sentence_chunks(chunks):
    """Split a VAD window's sorted chunks into sentence-length runs.
    A run closes when a chunk's text ends with SENTENCE_END_PUNCT or when
    the gap to the next chunk is >= SV_MIN_GAP_MS."""
    runs = []
    cur = []
    for i, c in enumerate(chunks):
        if cur and c[0] - cur[-1][1] >= SV_MIN_GAP_MS:
            runs.append(cur)
            cur = []
        cur.append(c)
        if _SENT_END_RE.search(c[2]):
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def transcribe_cues(wav_path, language="ja", return_windows=False):
    """SenseVoice + fsmn-vad transcription; sentence-boundary cue splitting
    (SENTENCE_END_PUNCT / SV_MIN_GAP_MS) + min-1s post-pass.
    Returns {start_ms, end_ms, text} cues (no cue < 1s; spans <= 6s).
    return_windows=True additionally returns (cues, windows) where windows
    is the per-VAD-window metadata used to build cues:
    [{"start_ms", "end_ms", "bgm", "text"}] — bgm True when any raw chunk in
    the window carried a <|BGM|> marker; text is the window's stripped text."""
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
    all_bgm = []
    for res in results:
        raw = res.get("text", "")
        timestamps = res.get("timestamp") or []
        chunks, bgms = _parse_chunks(raw, timestamps, capture_bgm=True)
        all_chunks.extend(chunks)
        all_bgm.extend(bgms)
    if not all_chunks:
        return ([], []) if return_windows else []

    vad = _vad_segments_ms(wav_path)

    buckets = {}
    for chunk, bgm in zip(all_chunks, all_bgm):
        best, best_ov = -1, 0.0
        for vi, (vs, ve) in enumerate(vad):
            ov = _overlap_ms(chunk[0], chunk[1], vs, ve)
            if ov > best_ov:
                best_ov, best = ov, vi
        bkey = best if best >= 0 else len(vad) + len(buckets)
        if bkey not in buckets:
            buckets[bkey] = {
                "bounds": vad[best] if best >= 0 else None,
                "chunks": [],
                "bgm": False,
            }
        buckets[bkey]["chunks"].append(chunk)
        if bgm:
            buckets[bkey]["bgm"] = True

    cues = []
    windows = []
    for vi in sorted(buckets):
        info = buckets[vi]
        chunks = sorted(info["chunks"], key=lambda c: c[0])
        if not chunks:
            continue
        bounds = info["bounds"]
        if bounds is not None:
            windows.append(
                {
                    "start_ms": bounds[0],
                    "end_ms": bounds[1],
                    "bgm": bool(info["bgm"]),
                    "text": " ".join(c[2] for c in chunks).strip(),
                }
            )
        for run in _split_sentence_chunks(chunks):
            cue = _assemble_cue(run)
            if not cue["text"]:
                continue
            if bounds is not None:
                # Clamp to the VAD window: SenseVoice char timestamps spill
                # tail chars into the next speech onset (56s monster spans);
                # the fsmn-vad window bounds are correct. Never emit zero/
                # negative spans — keep the pre-clamp values on inversion.
                nstart = max(cue["start"], bounds[0])
                nend = min(cue["end"], bounds[1])
                if nstart < nend:
                    cue["start"] = nstart
                    cue["end"] = nend
            cues.append(cue)
    cues.sort(key=lambda c: c["start"])
    cues = _min_dur_postpass(cues)
    if return_windows:
        return cues, windows
    return cues