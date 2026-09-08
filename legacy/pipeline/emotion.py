#!/usr/bin/env python3
"""Emotion pass: per-VAD-window emotion2vec+ classification for ASR cues.

Classifies each fsmn-vad window slice of the 16k mono wav with
emotion2vec_plus_large (9 classes) and attaches an English base label
("angry"/"sad"/"fearful"/"surprised"/... ) to the cues built from that
window. Pairs with pipeline/sensevoice.py transcribe_cues(return_windows=True).

Policies:
  - EMO_MODEL: model id (default emotion2vec/emotion2vec_plus_large).
  - EMO_CONF_MIN: minimum argmax score to accept (default 0.6).
  - EMO_MIN_MS: windows shorter than this are skipped (default 600).
  - EXCLUDED: labels never annotated — neutral/other/unk plus 开心/happy,
    which is a biased class for this model.
  - VETO: windows carrying a <|BGM|> marker with <= 1 char of text get no
    emotion (music/ambience, not speech); marked cue["emotion_veto"]=True.
  - EMO_CACHE: on-disk cache dir (~/.cache/asr-pipeline/emotion), keyed by
    sha1(wav mtime, window span, config hash incl. git HEAD). Read-through
    and write-through; never fails the pipeline.
Every entry point is failure-proof: model load failure, wav read failure and
per-window classification failures log a warning and yield no emotions —
the ASR/translate pipeline is identical with the pass disabled.
"""

import hashlib
import json
import os
import struct
import subprocess
import sys
import threading

import numpy as np

EMO_MODEL = os.environ.get("EMO_MODEL", "emotion2vec/emotion2vec_plus_large")
EMO_CONF_MIN = float(os.environ.get("EMO_CONF_MIN", "0.6"))
EMO_MIN_MS = int(os.environ.get("EMO_MIN_MS", "600"))
EMO_CACHE = os.path.expanduser(
    os.environ.get("EMO_CACHE", "~/.cache/asr-pipeline/emotion")
)
EXCLUDED = {"中立/neutral", "其他/other", "<unk>", "开心/happy"}

_LABEL_EN = {
    "生气/angry": "angry",
    "厌恶/disgusted": "disgusted",
    "恐惧/fearful": "fearful",
    "开心/happy": "happy",
    "中立/neutral": "neutral",
    "其他/other": "other",
    "难过/sad": "sad",
    "吃惊/surprised": "surprised",
}

_model_inst = None
_model_lock = threading.Lock()
_config_hash_cache = None


def _warn(msg):
    print(f"emotion: {msg}", file=sys.stderr)


def _model():
    """Lazy singleton AutoModel (emotion2vec+). Never raises: on load
    failure logs a warning and returns None."""
    global _model_inst
    if _model_inst is not None:
        return _model_inst
    with _model_lock:
        if _model_inst is None:
            try:
                from funasr import AutoModel

                _model_inst = AutoModel(
                    model=EMO_MODEL,
                    device="cuda:0",
                    hub="hf",
                    disable_update=True,
                )
            except Exception as e:
                _warn(f"model load failed ({e}); emotion pass disabled")
                return None
    return _model_inst


def _config_hash():
    """sha1 of sorted EMO_*/SV_* env values + repo git HEAD short (fallback
    'nohash'). Cached for the process lifetime."""
    global _config_hash_cache
    if _config_hash_cache is not None:
        return _config_hash_cache
    keys = sorted(
        k for k in os.environ if k.startswith("EMO_") or k.startswith("SV_")
    )
    blob = "\n".join(f"{k}={os.environ[k]}" for k in keys)
    try:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        head = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        head = ""
    _config_hash_cache = hashlib.sha1(
        (blob + "|" + (head or "nohash")).encode()
    ).hexdigest()
    return _config_hash_cache


def cache_key(wav_path, start_ms, end_ms):
    """Cache key for one window slice: sha1 of (wav mtime, span, config)."""
    try:
        mtime = os.path.getmtime(wav_path)
    except OSError:
        mtime = 0
    return hashlib.sha1(
        f"{wav_path}|{mtime:.0f}|{start_ms}|{end_ms}|{_config_hash()}".encode()
    ).hexdigest()


def _cache_read(key):
    try:
        with open(os.path.join(EMO_CACHE, key + ".json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _cache_write(key, label, score):
    try:
        os.makedirs(EMO_CACHE, exist_ok=True)
        tmp = os.path.join(EMO_CACHE, key + ".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"label": label, "score": score}, fh)
        os.replace(tmp, os.path.join(EMO_CACHE, key + ".json"))
    except OSError:
        pass


def _load_wav(path):
    """Read a 16kHz mono 16-bit PCM wav into (int16 ndarray, rate). Returns
    (None, 0) for anything else (never raises)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None, 0
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None, 0
    off = 12
    rate, channels, bits, pcm = 0, 0, 0, None
    while off + 8 <= len(data):
        cid = data[off:off + 4]
        size = struct.unpack("<I", data[off + 4:off + 8])[0]
        body = data[off + 8:off + 8 + size]
        if cid == b"fmt " and len(body) >= 16:
            f = struct.unpack("<HHIIHH", body[:16])
            channels, rate, bits = f[1], f[2], f[5]
        elif cid == b"data":
            pcm = body
            break
        off += 8 + size + (size & 1)
    if pcm is None or channels != 1 or bits != 16:
        return None, rate
    return np.frombuffer(pcm, dtype="<i2"), rate


def _write_wav(path, samples, rate):
    import wave

    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def _classify_slice(model, wav_path, start_ms, end_ms):
    """Slice the 16k mono PCM wav in memory and run emotion2vec utterance
    classification. Returns (cn_label, score) or (None, None) on any
    failure. Falls back to a temp wav file if the numpy-array input fails."""
    samples, rate = _load_wav(wav_path)
    if samples is None or rate != 16000:
        return None, None
    s = max(0, int(start_ms * rate / 1000))
    e = min(len(samples), int(end_ms * rate / 1000))
    if e - s < int(EMO_MIN_MS * rate / 1000):
        return None, None
    seg = samples[s:e]
    try:
        arr = (seg.astype(np.float32) / 32768.0)
        res = model.generate(
            input=arr,
            granularity="utterance",
            extract_embedding=False,
            disable_pbar=True,
        )
    except Exception:
        try:
            tdir = os.path.join(EMO_CACHE, "tmp")
            os.makedirs(tdir, exist_ok=True)
            tpath = os.path.join(tdir, f"{start_ms}-{end_ms}.wav")
            _write_wav(tpath, seg, rate)
            res = model.generate(
                input=tpath,
                granularity="utterance",
                extract_embedding=False,
                disable_pbar=True,
            )
        except Exception:
            return None, None
    try:
        labels = (res[0] or {}).get("labels") or []
        scores = (res[0] or {}).get("scores") or []
        if not labels or not scores:
            return None, None
        idx = max(range(len(scores)), key=scores.__getitem__)
        return labels[idx], float(scores[idx])
    except Exception:
        return None, None


def classify_window(model, wav_path, start_ms, end_ms):
    """Classify one window slice; returns (english_label, score) or
    (None, None) when below EMO_CONF_MIN, in EXCLUDED, or shorter than
    EMO_MIN_MS. Cache read-through/write-through."""
    if end_ms - start_ms < EMO_MIN_MS:
        return None, None
    key = cache_key(wav_path, start_ms, end_ms)
    hit = _cache_read(key)
    if hit:
        return hit.get("label"), hit.get("score")
    label_cn, score = _classify_slice(model, wav_path, start_ms, end_ms)
    if label_cn is None or score is None or score < EMO_CONF_MIN or label_cn in EXCLUDED:
        return None, None
    label = _LABEL_EN.get(label_cn)
    if label is None:
        return None, None
    _cache_write(key, label, score)
    return label, score


def classify_windows(wav_path, windows, enabled=True):
    """Classify every window; returns {(start_ms, end_ms): (label, score)}.
    Never raises: disabled, model unavailable or per-window failures simply
    skip windows."""
    if not enabled:
        return {}
    model = _model()
    if model is None:
        _warn("model unavailable; skipping emotion pass")
        return {}
    out = {}
    for w in windows or []:
        try:
            label, score = classify_window(
                model, wav_path, int(w["start_ms"]), int(w["end_ms"])
            )
        except Exception as e:
            _warn(f"window {w.get('start_ms')}-{w.get('end_ms')} failed ({e})")
            continue
        if label is not None:
            out[(int(w["start_ms"]), int(w["end_ms"]))] = (label, score)
    return out


def attach_emotion(cues, wav_path, windows_meta):
    """Attach "emotion" (English base label) to each cue from the FIRST
    window_meta whose [start_ms, end_ms) contains the cue's start; missing
    window match -> emotion None (synthetic fallback buckets have no
    window). BGM windows with <= 1 char of text are VETOED (emotion None,
    cue["emotion_veto"]=True). Every cue keeps an explicit "emotion" key.
    Never raises; returns cues."""
    windows = list(windows_meta or [])
    labels = classify_windows(wav_path, windows)
    for cue in cues:
        cue["emotion"] = None
        cue["emotion_veto"] = False
    for cue in cues:
        win = None
        for w in windows:
            if w["start_ms"] <= cue["start"] < w["end_ms"]:
                win = w
                break
        if win is None:
            continue
        if win.get("bgm") and len((win.get("text") or "").strip()) <= 1:
            cue["emotion_veto"] = True
            continue
        label, _score = labels.get((win["start_ms"], win["end_ms"]), (None, None))
        if label is None:
            continue
        cue["emotion"] = label
    return cues
