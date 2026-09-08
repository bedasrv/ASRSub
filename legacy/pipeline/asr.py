"""faster-whisper ASR backend for orchestrator v2.

Replaces the dead FunASR :9000 path. Runs faster-whisper large-v3-turbo int8
with Silero VAD (ja, condition_on_previous_text=False) and splits long whisper
segments into <=MAX_CUE_MS cues on internal silence/VAD boundaries.

VAD is configured with a VadOptions INSTANCE (faster_whisper.transcribe):
passing a plain dict silently overrides max_speech_duration_s with
chunk_length (default 30s), so the 8s cap never applied. When the installed
faster-whisper lacks VadOptions, fall back to chunk_length=8 + dict.
"""

import dataclasses
import os
import threading

MAX_CUE_MS = int(os.environ.get("MAX_CUE_MS", "8000"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")
WHISPER_BEAM = int(os.environ.get("WHISPER_BEAM", "5"))
VAD_THRESHOLD = float(os.environ.get("VAD_THRESHOLD", "0.5"))
VAD_MIN_SILENCE_MS = int(os.environ.get("VAD_MIN_SILENCE_MS", "200"))
VAD_MAX_SPEECH_S = float(os.environ.get("VAD_MAX_SPEECH_S", "8"))
VAD_MIN_SILENCE_AT_MAX = float(os.environ.get("VAD_MIN_SILENCE_AT_MAX", "98"))
VAD_SPEECH_PAD_MS = int(os.environ.get("VAD_SPEECH_PAD_MS", "250"))
SPLIT_MIN_SILENCE_MS = int(os.environ.get("SPLIT_MIN_SILENCE_MS", "500"))
HARD_GAP_MS = int(os.environ.get("HARD_GAP_MS", "1500"))
INITIAL_PROMPT = os.environ.get(
    "WHISPER_INITIAL_PROMPT", "こんにちは。これはアニメの台詞です。"
)

_VAD_KWARGS = {
    "threshold": VAD_THRESHOLD,
    "min_silence_duration_ms": VAD_MIN_SILENCE_MS,
    "max_speech_duration_s": VAD_MAX_SPEECH_S,
    "min_silence_at_max_speech": VAD_MIN_SILENCE_AT_MAX,
    "speech_pad_ms": VAD_SPEECH_PAD_MS,
}

try:
    from faster_whisper.transcribe import VadOptions

    _VAD_OPTIONS_CLS = VadOptions
except ImportError:
    _VAD_OPTIONS_CLS = None

_model = None
_model_lock = threading.Lock()


def get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(
                WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE
            )
    return _model


def reset_model():
    global _model
    with _model_lock:
        _model = None


def _word_gap_ms(w_prev, w_next):
    return int((w_next.start - w_prev.end) * 1000)


def _split_segment(words, seg_start_ms, seg_end_ms):
    """Group a whisper segment's words into cues of at most MAX_CUE_MS.

    - hard silence (word gap >= HARD_GAP_MS) always cuts;
    - a run that would exceed MAX_CUE_MS cuts at the last internal silence gap
      (>= VAD_MIN_SILENCE_MS) inside the window, else mid-run at the 8s cap;
    - a single word longer than MAX_CUE_MS (deliberate scream/music) is kept
      whole as its own cue.
    Returns list of dicts {start_ms, end_ms, text}; empty text when no words.
    """
    if not words:
        return [{"start": seg_start_ms, "end": seg_end_ms, "text": ""}]

    def flush(ws):
        return {
            "start": int(ws[0].start * 1000),
            "end": int(ws[-1].end * 1000),
            "text": "".join(wc.word for wc in ws).strip(),
        }

    cues = []
    cur = []
    for w in words:
        if not (w.word or "").strip():
            continue
        if cur:
            gap = _word_gap_ms(cur[-1], w)
            span_with = int(w.end * 1000) - int(cur[0].start * 1000)
            if gap >= HARD_GAP_MS:
                cues.append(flush(cur))
                cur = []
            elif span_with > MAX_CUE_MS:
                cut = None
                for i in range(len(cur) - 1, 0, -1):
                    if _word_gap_ms(cur[i - 1], cur[i]) >= SPLIT_MIN_SILENCE_MS:
                        cut = i
                        break
                if cut is not None:
                    cues.append(flush(cur[:cut]))
                    cur = cur[cut:]
                else:
                    cues.append(flush(cur))
                    cur = []
        cur.append(w)
    if cur:
        cues.append(flush(cur))
    return cues


def vad_parameters():
    """VadOptions instance (real 8s speech cap) or None when faster-whisper
    lacks it. Kwargs are filtered to the dataclass fields available in the
    installed version (e.g. min_silence_at_max_speech missing in 1.2.1)."""
    if _VAD_OPTIONS_CLS is None:
        return None
    fields = {f.name for f in dataclasses.fields(_VAD_OPTIONS_CLS)}
    kwargs = {k: v for k, v in _VAD_KWARGS.items() if k in fields}
    return _VAD_OPTIONS_CLS(**kwargs)


def ensure_contiguous(cues):
    """Post-pass: never overlap — next.start = max(next.start, prev.end).
    Cues are sorted by start; returns the same list, sorted + clamped."""
    cues.sort(key=lambda c: c["start"])
    for i in range(1, len(cues)):
        if cues[i]["start"] < cues[i - 1]["end"]:
            cues[i]["start"] = cues[i - 1]["end"]
        if cues[i]["end"] < cues[i]["start"]:
            cues[i]["end"] = cues[i]["start"]
    return cues


def transcribe_cues(wav_path, language="ja"):
    """Transcribe wav with faster-whisper + VAD; return cue dicts
    {start_ms, end_ms, text} with all segments split to <=MAX_CUE_MS and
    non-overlapping boundaries."""
    model = get_model()
    kwargs = dict(
        language=language,
        vad_filter=True,
        condition_on_previous_text=False,
        beam_size=WHISPER_BEAM,
        word_timestamps=True,
        initial_prompt=INITIAL_PROMPT,
    )
    vp = vad_parameters()
    if vp is not None:
        kwargs["vad_parameters"] = vp
    else:
        # old faster-whisper: dict silently overrides max_speech_duration_s
        # with chunk_length (30s default), so cap chunks explicitly.
        kwargs["vad_parameters"] = dict(min_silence_duration_ms=VAD_MIN_SILENCE_MS)
        kwargs["chunk_length"] = VAD_MAX_SPEECH_S
    segments, _info = model.transcribe(wav_path, **kwargs)
    cues = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        words = list(getattr(seg, "words", None) or [])
        s_ms = int(seg.start * 1000)
        e_ms = int(seg.end * 1000)
        split = _split_segment(words, s_ms, e_ms)
        if len(split) == 1 and not split[0]["text"]:
            split[0]["text"] = text
        elif sum(len(c["text"]) for c in split) < len(text) * 0.6:
            split = [{"start": s_ms, "end": e_ms, "text": text}]
        cues.extend(split)
    return ensure_contiguous(cues)
