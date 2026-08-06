#!/usr/bin/env python3
"""AI subtitle pipeline orchestrator: wanted list -> ASR -> LLM translate -> Bazarr upload.

Self-looping daemon (systemd Restart=always; flock guards single instance). Idempotent via JSONL state file.
"""

import collections
import fcntl
import hashlib
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOG_LOCK = threading.Lock()
_LOG_RING = collections.deque(maxlen=25)

ENV_FILE = "/home/user/.config/asr-pipeline/pipeline.env"
STATE_FILE = os.environ.get("STATE_FILE", "/home/user/.config/asr-pipeline/state.jsonl")

TRANSLATE_PROMPT = (
    "You are a professional anime subtitle translator. Translate the provided Japanese "
    "subtitle lines into {target_language}. Rules: (1) output ONLY a JSON array of strings, "
    "same count and order as input; (2) natural dialogue, keep honorifics and name suffixes "
    "(san/chan/kun); (3) each line <=42 characters; (4) do not add anything not in the "
    "source; (5) no timestamps, no numbering."
)

BATCH_SIZE = 20
TIMEOUT = 900
MODEL_FALLBACKS = [
    m.strip()
    for m in os.environ.get("TRANSLATE_FALLBACK_MODELS", "").split(",")
    if m.strip()
]
MAX_WORKERS = int(os.environ.get("MAX_TRANSLATE_WORKERS", "1"))
WAKE_EVENT = threading.Event()
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8085"))
OVERRIDE_FILE = "/home/user/.config/asr-pipeline/config.overrides.json"
CONTROL_API_KEY = os.environ.get("CONTROL_API_KEY", "")
ASR_CACHE_DIR = os.environ.get(
    "ASR_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "asr-pipeline", "asr"),
)

TRANSLATE_BASE = os.environ.get("TRANSLATE_BASE", "http://127.0.0.1:8011/v1")
TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "HY-MT1.5-1.8B-Q4_K_M.gguf")
LOCAL_CHUNK_SIZE = 40
LANG_NAMES = {"id": "Indonesian", "en": "English"}
KANA_RE = re.compile(r"[\u3040-\u30ff]")
HANZI_RE = re.compile(r"[\u3400-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
CTX_OVERFLOW = "__CTX_OVERFLOW__"
HY_STOP_TOKENS = ["<｜hy_place▁holder▁no▁2｜>", "<｜hy_end▁of▁sentence｜>"]

_stop_requested = False
_paused = False
_run_once_requested = False
_last_pass_stats = None
_started_at = datetime.now(timezone.utc)
_consecutive_failures = 0
_HELP_COOLDOWN = {}
_GLOBAL_HELP_LAST = 0.0


def _handle_sigterm(signum, frame):
    global _stop_requested
    _stop_requested = True
    WAKE_EVENT.set()
    log("SIGTERM received, finishing current pass then exiting")


def load_config():
    cfg = {}
    with open(ENV_FILE, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
    if os.path.exists(OVERRIDE_FILE):
        try:
            with open(OVERRIDE_FILE, "r", encoding="utf-8") as fh:
                ov = json.loads(fh.read())
            if isinstance(ov, dict):
                for k, v in ov.items():
                    cfg[str(k)] = str(v)
        except Exception as exc:
            log(f"WARNING: failed to load {OVERRIDE_FILE}: {exc}")
    cfg["MAX_EPS_PER_RUN"] = int(
        os.environ.get("MAX_EPS_PER_RUN", cfg.get("MAX_EPS_PER_RUN", "8"))
    )
    cfg["TARGET_LANGS"] = [
        x.strip() for x in cfg.get("TARGET_LANGS", "id,en").split(",") if x.strip()
    ]
    cfg["STATE_FILE"] = STATE_FILE
    return cfg


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _LOG_LOCK:
        _LOG_RING.append(f"{ts} {msg}")
        print(f"{ts} {msg}", flush=True)


# ---------- state ----------


def load_state():
    entries = []
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
    return entries


def append_state(entry):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    entry["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(STATE_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------- remote APIs ----------


def get_wanted(cfg):
    r = requests.get(
        cfg["BAZARR_URL"].rstrip("/") + "/episodes/wanted",
        params={"start": 0, "length": 500},
        headers={"X-API-KEY": cfg["BAZARR_API_KEY"]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def get_episode(cfg, ep_id):
    r = requests.get(
        cfg["SONARR_URL"].rstrip("/") + f"/episode/{ep_id}",
        headers={"X-Api-Key": cfg["SONARR_API_KEY"]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def map_path(container_path):
    if container_path.startswith("/data/"):
        return "/mnt/nas/share/media/" + container_path[len("/data/") :]
    return container_path


def probe_audio(path):
    p = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_name,codec_type:stream_tags=language",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {p.stderr.strip()}")
    data = json.loads(p.stdout)
    return [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]


def choose_source(streams, target_lang):
    if not streams:
        return None
    lang_of = lambda s: s.get("tags", {}).get("language")
    if target_lang == "en":
        eng = next((s for s in streams if lang_of(s) == "eng"), None)
        if eng is not None:
            return {
                "stream_index": eng["index"],
                "asr_lang": "en",
                "needs_translate": False,
                "src_lang": "eng",
            }
    jpn = next((s for s in streams if lang_of(s) == "jpn"), None)
    s = jpn if jpn is not None else streams[0]
    return {
        "stream_index": s["index"],
        "asr_lang": "ja",
        "needs_translate": True,
        "src_lang": lang_of(s) or "?",
    }


def extract_wav(path, stream_index, out_path):
    p = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            path,
            "-map",
            f"0:{stream_index}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            out_path,
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg extract failed: {p.stderr.strip()}")


def _media_fingerprint(path):
    st = os.stat(path)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def asr_cache_get(ep_id, asr_lang, media_path):
    key = f"{ep_id}_{asr_lang}"
    srt_path = os.path.join(ASR_CACHE_DIR, key + ".srt")
    meta_path = os.path.join(ASR_CACHE_DIR, key + ".json")
    try:
        with open(meta_path) as fh:
            meta = json.load(fh)
        if meta.get("fingerprint") == _media_fingerprint(media_path):
            with open(srt_path) as fh:
                text = fh.read()
            if text.strip():
                return text
    except Exception:
        pass
    return None


def asr_cache_put(ep_id, asr_lang, media_path, text):
    os.makedirs(ASR_CACHE_DIR, exist_ok=True)
    key = f"{ep_id}_{asr_lang}"
    with open(os.path.join(ASR_CACHE_DIR, key + ".srt"), "w") as fh:
        fh.write(text)
    with open(os.path.join(ASR_CACHE_DIR, key + ".json"), "w") as fh:
        json.dump({"fingerprint": _media_fingerprint(media_path)}, fh)


def asr_srt(cfg, wav_path, lang):
    with open(wav_path, "rb") as fh:
        r = requests.post(
            cfg["ASR_URL"].rstrip("/") + "/asr",
            params={
                "task": "transcribe",
                "language": lang,
                "output": "srt",
                "encode": "false",
            },
            files={"audio_file": fh},
            timeout=600,
        )
    if r.status_code != 200:
        raise RuntimeError(f"ASR HTTP {r.status_code}: {r.text[:200]}")
    return r.text


# ---------- SRT ----------


def parse_srt(text):
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        ts_idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ts_idx is None:
            continue
        m = re.match(
            r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})",
            lines[ts_idx],
        )
        if not m:
            continue
        body = " ".join(lines[ts_idx + 1 :]).strip()
        if not body:
            continue
        cues.append({"start": m.group(1), "end": m.group(2), "text": body})
    return cues


def write_srt(cues, texts, out_path):
    with open(out_path, "w", encoding="utf-8") as fh:
        for i, (cue, text) in enumerate(zip(cues, texts), 1):
            fh.write(f"{i}\n{cue['start']} --> {cue['end']}\n{text}\n\n")


# ---------- translation (Zen Go, OpenAI-compatible) ----------


def extract_json_array(raw):
    if not raw:
        return None
    s = raw.strip()
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return None
    for cand in (s[i : j + 1], raw[i : j + 1]):
        try:
            arr = json.loads(cand)
            if isinstance(arr, list):
                return [str(x) for x in arr]
        except Exception:
            continue
    return None


def post_chat(cfg, messages, model, key, local=False):
    url = (cfg.get("TRANSLATE_BASE") or TRANSLATE_BASE).rstrip(
        "/"
    ) + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if local:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "top_k": 20,
            "top_p": 0.6,
            "repeat_penalty": 1.0,
            "max_tokens": 8192,
            "stop": HY_STOP_TOKENS,
        }
    else:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.3,
            "thinking": {"type": "enabled", "effort": "max"},
        }
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
        except requests.RequestException as exc:
            log(f"    [translate] network error (attempt {attempt + 1}): {exc}")
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                return r.json()["choices"][0]["message"]["content"]
            except Exception:
                return None
        if r.status_code == 404:
            return None  # signal model fallback
        if (
            local
            and r.status_code == 400
            and "exceeds the available context size" in r.text
        ):
            # deterministic context overflow: retrying the same size always fails.
            # Signal the caller to split the chunk instead of deferring.
            log(
                f"    [translate] HTTP 400 context overflow (attempt {attempt + 1}): {r.text[:200]} - splitting chunk"
            )
            return CTX_OVERFLOW
        # 5xx/429: server overload or rate limit - do not hammer; defer to next pass
        log(
            f"    [translate] HTTP {r.status_code} (attempt {attempt + 1}): {r.text[:200]} - deferring to next pass"
        )
        return None
    return None


def get_prior_context(cfg, series_id, episode_id):
    try:
        r = requests.get(
            cfg["SONARR_URL"].rstrip("/") + "/episode",
            params={"seriesId": series_id},
            headers={"X-Api-Key": cfg["SONARR_API_KEY"]},
            timeout=60,
        )
        r.raise_for_status()
        current = get_episode(cfg, episode_id)
        season = current.get("seasonNumber")
        episode_num = current.get("episodeNumber")
        if not isinstance(season, int) or not isinstance(episode_num, int):
            return []
        prev_nums = {episode_num - 1, episode_num - 2}
        prev_eps = [
            e
            for e in r.json()
            if e.get("seasonNumber") == season and e.get("episodeNumber") in prev_nums
        ]
        lines = []
        seen = set()
        for prev in sorted(prev_eps, key=lambda e: -e["episodeNumber"]):
            if len(lines) >= 20:
                break
            try:
                sr = requests.get(
                    cfg["BAZARR_URL"].rstrip("/") + "/episodes",
                    params={"episodeid[]": prev["id"]},
                    headers={"X-API-KEY": cfg["BAZARR_API_KEY"]},
                    timeout=60,
                )
                if sr.status_code != 200:
                    continue
                data = sr.json()
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            items = data.get("data") or []
            if not items:
                continue
            subs = items[0].get("subtitles") or []
            subs.sort(key=lambda s: 0 if (s.get("code2") or "") == "id" else 1)
            chosen = None
            for s in subs:
                p = s.get("path")
                if not p:
                    continue
                local = map_path(p)
                if os.path.isfile(local):
                    chosen = local
                    break
            if not chosen:
                continue
            try:
                with open(chosen, "r", encoding="utf-8", errors="replace") as fh:
                    srt_text = fh.read()
                for cue in parse_srt(srt_text):
                    t = cue["text"]
                    if t and t not in seen:
                        seen.add(t)
                        lines.append(t)
                        if len(lines) >= 40:
                            break
            except Exception:
                continue
        return lines[:20]
    except Exception:
        return []


def is_local_translate(cfg):
    base = cfg.get("TRANSLATE_BASE") or TRANSLATE_BASE
    return "127.0.0.1" in base or "localhost" in base


def _parse_numbered_response(raw):
    if not raw:
        return None
    parsed = {}
    for line in raw.splitlines():
        m = re.match(r"^(\d+)\.\s*(.+)$", line.strip())
        if m:
            parsed[int(m.group(1))] = m.group(2).strip()
    return parsed


def sanitize_lines(lines, max_repeat=10):
    """Collapse runs of >max_repeat identical chars to max_repeat + ellipsis
    (anime scream lines otherwise trigger repetition collapse in small models)."""
    out = []
    for s in lines:
        if not s:
            out.append(s)
            continue
        parts = []
        i = 0
        while i < len(s):
            j = i
            while j < len(s) and s[j] == s[i]:
                j += 1
            run = j - i
            if run > max_repeat:
                parts.append(s[i] * max_repeat + "…")
            else:
                parts.append(s[i:j])
            i = j
        out.append("".join(parts))
    return out


def guard_foreign_lines(lines):
    """Replace foreign-script lines (Chinese w/o kana, mostly-latin English) with a
    clean Japanese placeholder so the 1.8B model does not echo the whole chunk.
    Returns (guarded_lines, [(original_index, original_text), ...]) for swap-back."""
    out, foreign = [], []
    for i, l in enumerate(lines):
        latin_chars = LATIN_RE.findall(l)
        latin_ratio = len(latin_chars) / max(len(l), 1)
        if (HANZI_RE.search(l) and not KANA_RE.search(l)) or (
            latin_ratio > 0.5 and not KANA_RE.search(l)
        ):
            foreign.append((i, l))
            out.append("\uff08\u6b4c\u8a5e\uff09")  # （歌詞）
        else:
            out.append(l)
    return out, foreign


def _local_chat_chunk(cfg, lines, target_lang, key, context_lines=None, depth=0):
    if context_lines:
        context_lines = context_lines[:20]
    n = len(lines)
    lang_name = LANG_NAMES.get(target_lang, target_lang)
    system = (
        f"Translate each line into {lang_name}. Reply as numbered list, "
        f"e.g. 1. ... 2. ... 3. ..., exactly {n} lines, no extra text."
    )
    if context_lines:
        system += (
            "\n\nUse these previously translated lines from earlier episodes for "
            "consistent names, terms, and style:\n"
            + "\n".join(f"REF: {l}" for l in context_lines)
        )
    prompt = system + "\n\n" + "\n".join(f"{i}. {l}" for i, l in enumerate(lines, 1))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    model = cfg.get("TRANSLATE_MODEL") or TRANSLATE_MODEL
    for _ in range(3):  # initial + up to 2 corrective retries
        raw = post_chat(cfg, messages, model, key, local=True)
        if raw == CTX_OVERFLOW:
            if depth >= 2 or n <= 1:
                log("    [translate] context overflow even after splitting; giving up")
                return None
            mid = n // 2
            log(
                f"    [translate] context overflow on {n}-line chunk; splitting in half ({mid}+{n - mid})"
            )
            left = _local_chat_chunk(
                cfg, lines[:mid], target_lang, key, context_lines, depth + 1
            )
            if left is None:
                return None
            right = _local_chat_chunk(
                cfg, lines[mid:], target_lang, key, context_lines, depth + 1
            )
            if right is None:
                return None
            return left + right
        if raw is None:
            log("    [translate] local endpoint returned nothing")
            return None
        parsed = _parse_numbered_response(raw)
        if parsed is not None and len(parsed) == n:
            first10 = [parsed.get(i, "") for i in range(1, min(11, n + 1))]
            if any(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", t) for t in first10):
                if depth < 2 and n > 1:
                    mid = n // 2
                    log(
                        f"    [translate] output is source echo (CJK), not {target_lang}; splitting in half ({mid}+{n - mid})"
                    )
                    left = _local_chat_chunk(
                        cfg, lines[:mid], target_lang, key, context_lines, depth + 1
                    )
                    if left is None:
                        return None
                    right = _local_chat_chunk(
                        cfg, lines[mid:], target_lang, key, context_lines, depth + 1
                    )
                    if right is None:
                        return None
                    return left + right
                log(
                    f"    [translate] output is source echo (CJK), not {target_lang} even after splitting; failing"
                )
                return None
            return [parsed.get(i, "") for i in range(1, n + 1)]
        if parsed is not None and len(parsed) < n and depth < 2 and n > 1:
            mid = n // 2
            log(
                f"    [translate] output truncated (got {len(parsed)}, want {n}) on {n}-line chunk; splitting in half ({mid}+{n - mid})"
            )
            left = _local_chat_chunk(
                cfg, lines[:mid], target_lang, key, context_lines, depth + 1
            )
            if left is None:
                return None
            right = _local_chat_chunk(
                cfg, lines[mid:], target_lang, key, context_lines, depth + 1
            )
            if right is None:
                return None
            return left + right
        log(
            f"    [translate] local count mismatch got {len(parsed) if parsed else 0}, want {n}"
        )
        messages.append({"role": "assistant", "content": raw})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"You forgot line numbering or skipped lines. Reply as numbered list, "
                    f"e.g. 1. ... 2. ... 3. ..., exactly {n} lines, no extra text."
                ),
            }
        )
    log("    [translate] local unparseable / count mismatch after retry")
    return None


def _local_translate_batch(cfg, lines, target_lang, key, context_lines=None):
    out = []
    for start in range(0, len(lines), LOCAL_CHUNK_SIZE):
        chunk = lines[start : start + LOCAL_CHUNK_SIZE]
        translated = _local_chat_chunk(cfg, chunk, target_lang, key, context_lines)
        if translated is None:
            return None
        out.extend(translated)
    return out


def chat_translate_batch(cfg, lines, target_lang, key, context_lines=None):
    lines = sanitize_lines(lines)
    if is_local_translate(cfg):
        # local 1.8B model echoes target-language REF lines verbatim (verified
        # 12-40/100 echo at any ref count); prior context only for cloud path
        lines, foreign = guard_foreign_lines(lines)
        out = _local_translate_batch(cfg, lines, target_lang, key, None)
        if out is None:
            return None
        for idx, orig in foreign:
            out[idx] = orig
        return out
    models = [cfg.get("TRANSLATE_MODEL") or TRANSLATE_MODEL] + [
        m for m in MODEL_FALLBACKS if m != cfg.get("TRANSLATE_MODEL")
    ]
    system = TRANSLATE_PROMPT.format(target_language=target_lang)
    if context_lines:
        system += (
            "\n\nUse these previously translated lines from earlier episodes for "
            "consistent names, terms, and style:\n"
            + "\n".join(f"REF: {l}" for l in context_lines)
        )
    user = json.dumps(
        {"target_language": target_lang, "lines": lines}, ensure_ascii=False
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    for _ in range(2):  # one corrective retry on parse failure
        raw = None
        for model in models:
            content = post_chat(cfg, messages, model, key)
            if content is None:
                continue  # 404 -> next model, or network dead -> try next model too
            raw = content
            break
        if raw is None:
            log("    [translate] all models/attempts failed (network or 404)")
            return None
        parsed = extract_json_array(raw)
        if parsed is not None and len(parsed) == len(lines):
            return parsed
        messages.append({"role": "assistant", "content": raw})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Your previous output was not a JSON array with exactly {len(lines)} "
                    'strings. Output ONLY the JSON array, e.g. ["...", "..."], same count '
                    "and order as the input."
                ),
            }
        )
    log("    [translate] unparseable / count mismatch after retry")
    return None


def translate_texts(
    cfg, cues, target_lang, key, series_id=None, episode_id=None, prior_cache=None
):
    lines = [c["text"] for c in cues]
    if series_id and episode_id and prior_cache is not None:
        if series_id not in prior_cache:
            prior_cache[series_id] = get_prior_context(cfg, series_id, episode_id)
        context_lines = prior_cache[series_id]
    else:
        context_lines = (
            get_prior_context(cfg, series_id, episode_id)
            if series_id and episode_id
            else []
        )
    out = chat_translate_batch(cfg, lines, target_lang, key, context_lines)
    if out is None or len(out) != len(lines):
        return None
    return out


# ---------- upload ----------


def upload_srt(cfg, series_id, ep_id, lang, srt_bytes):
    url = cfg["BAZARR_URL"].rstrip("/") + "/episodes/subtitles"
    params = {
        "seriesid": series_id,
        "episodeid": ep_id,
        "language": lang,
        "forced": "false",
        "hi": "false",
    }
    headers = {"X-API-KEY": cfg["BAZARR_API_KEY"]}
    files = {"file": ("sub.srt", srt_bytes, "application/x-subrip")}
    last_code = None
    for attempt in range(3):
        try:
            r = requests.post(
                url, params=params, headers=headers, files=files, timeout=120
            )
            last_code = r.status_code
            if r.status_code == 204:
                return 204
            log(
                f"    [upload] HTTP {r.status_code} (attempt {attempt + 1}): {r.text[:200]}"
            )
        except requests.RequestException as exc:
            last_code = None
            log(f"    [upload] network error (attempt {attempt + 1}): {exc}")
        if attempt < 2:
            time.sleep(5 * (attempt + 1))
    return last_code


def notify_hermes(cfg, ep_id, lang, series, tag, exc):
    global _GLOBAL_HELP_LAST
    try:
        url = cfg.get("HERMES_WEBHOOK_URL", "")
        secret = cfg.get("HERMES_WEBHOOK_SECRET", "")
        if not url or not secret:
            return
        now = time.time()
        last = _HELP_COOLDOWN.get((ep_id, lang), 0)
        if now - last < 1800:
            return
        if now - _GLOBAL_HELP_LAST < 600:
            log("help: throttled (global)")
            return
        payload = {
            "event_type": "pipeline_error",
            "episode": f"{tag} {series}",
            "lang": lang,
            "error": str(exc)[:500],
            "log_tail": list(_LOG_RING),
        }
        body = json.dumps(payload).encode()
        ts = str(int(time.time()))
        sig = hmac.new(
            secret.encode(), ts.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        headers = {
            "X-Webhook-Signature-V2": sig,
            "X-Webhook-Timestamp": ts,
            "Content-Type": "application/json",
        }
        r = requests.post(url, data=body, headers=headers, timeout=10)
        if r.status_code in (200, 202):
            _HELP_COOLDOWN[(ep_id, lang)] = time.time()
            _GLOBAL_HELP_LAST = time.time()
            log(f"help: {tag} {series} [{lang}] notified Hermes")
        else:
            log(f"help: notify failed: HTTP {r.status_code}")
    except Exception as e:
        log(f"help: notify failed: {e}")


def process_after_asr(
    cfg,
    key,
    ep_id,
    lang,
    series,
    tag,
    src,
    t0,
    cues,
    decision,
    info,
    wav_path,
    srt_path,
    prior_cache=None,
):
    global _paused
    try:
        if decision["needs_translate"]:
            t_tr = time.time()
            if not key:
                raise RuntimeError("translate needed but TRANSLATE_API_KEY empty")
            texts = translate_texts(
                cfg,
                cues,
                lang,
                key,
                info.get("seriesId") or info.get("sonarrSeriesId"),
                ep_id,
                prior_cache,
            )
            if texts is None or len(texts) != len(cues):
                raise RuntimeError("translation failed / count mismatch")
            tr_elapsed = time.time() - t_tr
            log(
                f"  {tag} [{src}->{lang}] translate {tr_elapsed:.0f}s, {len(texts)} lines"
            )
        else:
            texts = [c["text"] for c in cues]

        write_srt(cues, texts, srt_path)
        with open(srt_path, "rb") as fh:
            srt_bytes = fh.read()
        code = upload_srt(
            cfg,
            info.get("seriesId") or info.get("sonarrSeriesId"),
            ep_id,
            lang,
            srt_bytes,
        )
        if code != 204:
            raise RuntimeError(f"upload HTTP {code} (expected 204)")
        elapsed = round(time.time() - t0, 1)
        append_state(
            {
                "sonarrEpisodeId": ep_id,
                "language": lang,
                "status": "done",
                "elapsed_s": elapsed,
            }
        )
        log(f"done: {tag} {series} [{src}->{lang}] in {elapsed}s (upload 204)")
        for f in (wav_path, srt_path):
            try:
                os.remove(f)
            except OSError:
                pass
        return "done"
    except Exception as exc:
        append_state(
            {
                "sonarrEpisodeId": ep_id,
                "language": lang,
                "status": "error",
                "elapsed_s": round(time.time() - t0, 1),
            }
        )
        log(f"fail: {series} [{lang}] {exc}")
        notify_hermes(cfg, ep_id, lang, series, tag, exc)
        halt_on_error(cfg, "process_episode", f"{series} [{lang}] {tag}", exc)
        return "failed"


# ---------- main ----------


def halt_on_error(cfg, stage, ep_desc, exc, extra=None):
    global _paused
    _paused = True
    rep = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stage": stage,
        "episode": ep_desc,
        "error": str(exc)[:500] or type(exc).__name__,
        "extra": extra or {},
    }
    report_dir = os.path.join(os.path.expanduser("~"), ".config", "asr-pipeline")
    os.makedirs(report_dir, exist_ok=True)
    dst = os.path.join(report_dir, "last_error.json")
    tmp = dst + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rep, fh, indent=2)
    os.replace(tmp, dst)
    try:
        hist = os.path.join(report_dir, "error_history")
        os.makedirs(hist, exist_ok=True)
        import shutil

        shutil.copy2(
            dst, os.path.join(hist, f"error_{time.strftime('%Y%m%d_%H%M%S')}.json")
        )
    except Exception:
        pass
    log(
        f"[pipeline] PAUSED on error - fix first, resume via pctl resume (report: {dst})"
    )


def run_pass():
    global _paused
    cfg = load_config()
    target_langs = set(cfg["TARGET_LANGS"])
    key = cfg.get("TRANSLATE_API_KEY", "")
    if not key:
        log(
            "WARNING: TRANSLATE_API_KEY is empty; any episode needing translation will FAIL"
        )

    entries = load_state()

    done_keys = set()
    consec_errors = {}
    last_entry = {}
    for e in entries:
        k = (e.get("sonarrEpisodeId"), e.get("language"))
        last_entry[k] = e
        if e.get("status") == "error":
            consec_errors[k] = consec_errors.get(k, 0) + 1
        else:
            consec_errors[k] = 0
    done_keys = {k for k, e in last_entry.items() if e.get("status") == "done"}

    try:
        wanted = get_wanted(cfg)
    except Exception as exc:
        log(f"ERROR fetching wanted list: {exc}")
        return {"fetch_error": True}
    total = wanted.get("total", 0)
    wanted_before = total

    candidates = []
    seen = set()
    for item in wanted.get("data", []):
        ep_id = item.get("sonarrEpisodeId")
        if ep_id in seen:
            continue
        missing = {m.get("code2") for m in item.get("missing_subtitles", [])}
        if missing & target_langs:
            candidates.append(item)
            seen.add(ep_id)
    candidates.sort(key=lambda x: x["sonarrEpisodeId"])
    candidates = candidates[: cfg["MAX_EPS_PER_RUN"]]

    processed = done = skipped = failed = 0
    log(
        f"wanted total={total}, candidates for pass={len(candidates)} "
        f"(max={cfg['MAX_EPS_PER_RUN']})"
    )

    prior_cache = {}
    futures = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for item in candidates:
            ep_id = item["sonarrEpisodeId"]
            series = item.get("seriesTitle", "?")
            missing = {m.get("code2") for m in item.get("missing_subtitles", [])}
            langs = sorted(missing & target_langs)
            for lang in langs:
                if (ep_id, lang) in done_keys:
                    log(f"skip: S?E? {series} [{lang}] already done (state)")
                    skipped += 1
                    continue
                if consec_errors.get((ep_id, lang), 0) >= 2:
                    le = last_entry.get((ep_id, lang)) or {}
                    last_ts = le.get("ts", "")
                    try:
                        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                        age_s = (datetime.now(timezone.utc) - last_dt).total_seconds()
                    except Exception:
                        age_s = 0
                    if age_s < 1800:
                        log(
                            f"park: {series} [{lang}] parked 30min after consecutive errors"
                        )
                        skipped += 1
                        continue
                processed += 1
                t0 = time.time()
                try:
                    info = get_episode(cfg, ep_id)
                    season = info.get("seasonNumber", "?")
                    episode_num = info.get("episodeNumber", "?")
                    tag = (
                        f"S{season:02d}E{episode_num:02d}"
                        if isinstance(season, int) and isinstance(episode_num, int)
                        else f"S{season}E{episode_num}"
                    )
                    ef = info.get("episodeFile") or {}
                    if not info.get("hasFile") or not ef.get("path"):
                        log(f"skip: {tag} {series} [{lang}] hasFile=false or no path")
                        skipped += 1
                        continue
                    container_path = ef["path"]
                    media_path = map_path(container_path)
                    if not os.path.isfile(media_path):
                        log(
                            f"skip: {tag} {series} [{lang}] file missing on NFS: {media_path}"
                        )
                        skipped += 1
                        continue
                    streams = probe_audio(media_path)
                    decision = choose_source(streams, lang)
                    if decision is None:
                        log(f"fail: {tag} {series} [{lang}] no audio streams")
                        notify_hermes(
                            cfg,
                            ep_id,
                            lang,
                            series,
                            tag,
                            RuntimeError("no audio streams"),
                        )
                        halt_on_error(
                            cfg,
                            "no_audio_streams",
                            f"{series} [{lang}] {tag}",
                            RuntimeError("no audio streams"),
                        )
                        failed += 1
                        append_state(
                            {
                                "sonarrEpisodeId": ep_id,
                                "language": lang,
                                "status": "error",
                                "elapsed_s": round(time.time() - t0, 1),
                            }
                        )
                        continue
                    src = decision["src_lang"]
                    log(
                        f"proc: {tag} {series} [{src}->{lang}] source_stream={decision['stream_index']} asr_lang={decision['asr_lang']} needs_translate={decision['needs_translate']}"
                    )

                    os.makedirs(cfg["TMP_DIR"], exist_ok=True)
                    wav_path = os.path.join(cfg["TMP_DIR"], f"{ep_id}_{lang}.wav")
                    srt_path = os.path.join(cfg["TMP_DIR"], f"{ep_id}_{lang}.srt")

                    cached_text = asr_cache_get(ep_id, decision["asr_lang"], media_path)
                    if cached_text:
                        srt_text = cached_text
                        log(f"  {tag} [{src}->{lang}] ASR cache hit (skip extract+asr)")
                    else:
                        extract_wav(media_path, decision["stream_index"], wav_path)
                        t_asr = time.time()
                        srt_text = asr_srt(cfg, wav_path, decision["asr_lang"])
                        asr_elapsed = time.time() - t_asr
                        asr_cache_put(ep_id, decision["asr_lang"], media_path, srt_text)
                    cues = parse_srt(srt_text)
                    if not cues:
                        raise RuntimeError("ASR returned no cues")
                    if not cached_text:
                        log(
                            f"  {tag} [{src}->{lang}] ASR {asr_elapsed:.0f}s, {len(cues)} cues"
                        )
                    futures.append(
                        pool.submit(
                            process_after_asr,
                            cfg,
                            key,
                            ep_id,
                            lang,
                            series,
                            tag,
                            src,
                            t0,
                            cues,
                            decision,
                            info,
                            wav_path,
                            srt_path,
                            prior_cache=prior_cache,
                        )
                    )
                except Exception as exc:
                    failed += 1
                    append_state(
                        {
                            "sonarrEpisodeId": ep_id,
                            "language": lang,
                            "status": "error",
                            "elapsed_s": round(time.time() - t0, 1),
                        }
                    )
                    log(f"fail: {series} [{lang}] {exc}")
                    notify_hermes(cfg, ep_id, lang, series, tag, exc)
                    halt_on_error(
                        cfg, "process_submit", f"{series} [{lang}] {tag}", exc
                    )

        for fut in as_completed(futures):
            if fut.result() == "done":
                done += 1
            else:
                failed += 1

    if processed == 0:
        wanted_after = wanted_before  # nothing changed; skip redundant API call
    else:
        try:
            wanted_after = get_wanted(cfg).get("total", 0)
        except Exception:
            wanted_after = None
    log(
        f"pass summary: processed={processed} done={done} skipped={skipped} failed={failed} "
        f"wanted_before={wanted_before} wanted_after={wanted_after}"
    )
    return {
        "processed": processed,
        "done": done,
        "skipped": skipped,
        "failed": failed,
        "wanted_before": wanted_before,
        "wanted_after": wanted_after,
    }


SECRET_KEY_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def _mask_secrets(cfg):
    return {
        k: ("***" if any(h in k.upper() for h in SECRET_KEY_HINTS) else v)
        for k, v in cfg.items()
    }


def _read_overrides():
    if not os.path.exists(OVERRIDE_FILE):
        return {}
    try:
        with open(OVERRIDE_FILE, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_overrides(ov):
    os.makedirs(os.path.dirname(OVERRIDE_FILE), exist_ok=True)
    tmp = OVERRIDE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(ov, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, OVERRIDE_FILE)


class ControlHandler(BaseHTTPRequestHandler):
    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        if not CONTROL_API_KEY:
            self._send_json(401, {"error": "CONTROL_API_KEY not configured"})
            return False
        if self.headers.get("X-API-Key") != CONTROL_API_KEY:
            self._send_json(401, {"error": "unauthorized"})
            return False
        return True

    def _state_counts(self):
        counts = {"done": 0, "error": 0, "pending": 0, "total": 0}
        for e in load_state():
            counts["total"] += 1
            st = e.get("status")
            if st in counts:
                counts[st] += 1
        return counts

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(200, {"ok": True})
        elif path == "/status":
            self._send_json(
                200,
                {
                    "uptime_s": int(
                        (datetime.now(timezone.utc) - _started_at).total_seconds()
                    ),
                    "paused": _paused,
                    "run_once_requested": _run_once_requested,
                    "last_pass": _last_pass_stats,
                    "state_counts": self._state_counts(),
                    "consecutive_failures": _consecutive_failures,
                    "started_at": _started_at.isoformat(),
                },
            )
        elif path == "/config":
            self._send_json(200, _mask_secrets(load_config()))
        elif path == "/sonarr-webhook":
            WAKE_EVENT.set()
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        global _paused, _run_once_requested
        path = self.path.split("?", 1)[0]
        if path == "/sonarr-webhook":
            WAKE_EVENT.set()
            self._send_json(200, {"ok": True})
            return
        if path == "/wake":
            WAKE_EVENT.set()
            self._send_json(200, {"ok": True})
            return
        if not self._check_auth():
            return
        if path == "/config":
            try:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception as exc:
                self._send_json(400, {"error": f"invalid JSON body: {exc}"})
                return
            if not isinstance(body, dict):
                self._send_json(400, {"error": "body must be a JSON object"})
                return
            ov = _read_overrides()
            applied = {}
            for k, v in body.items():
                if v is None:
                    ov.pop(k, None)
                    applied[k] = None
                else:
                    ov[str(k)] = str(v)
                    applied[str(k)] = str(v)
            _write_overrides(ov)
            WAKE_EVENT.set()
            self._send_json(200, {"ok": True, "applied": _mask_secrets(applied)})
        elif path == "/pause":
            _paused = True
            self._send_json(200, {"ok": True, "paused": True})
        elif path == "/resume":
            _paused = False
            WAKE_EVENT.set()
            try:
                report_dir = os.path.join(
                    os.path.expanduser("~"), ".config", "asr-pipeline"
                )
                dst = os.path.join(report_dir, "last_error.json")
                if os.path.exists(dst):
                    hist = os.path.join(report_dir, "error_history")
                    os.makedirs(hist, exist_ok=True)
                    os.replace(
                        dst,
                        os.path.join(
                            hist, f"error_{time.strftime('%Y%m%d_%H%M%S')}.json"
                        ),
                    )
                    log("cleared error report")
            except Exception:
                pass
            self._send_json(200, {"ok": True, "paused": False})
        elif path == "/run-once":
            _run_once_requested = True
            WAKE_EVENT.set()
            self._send_json(200, {"ok": True, "run_once_requested": True})
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


def start_webhook_listener():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", WEBHOOK_PORT), ControlHandler)
    except OSError as exc:
        log(
            f"webhook listener unavailable on :{WEBHOOK_PORT} ({exc}); continuing without it"
        )
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(
        f"control API + webhook listener on :{WEBHOOK_PORT} (GET /status, /config, /health; POST /config /pause /resume /run-once /wake /sonarr-webhook)"
    )


def main():
    lock_path = "/home/user/.config/asr-pipeline/orchestrator.lock"
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another orchestrator instance running, exiting")
        return 0
    global _consecutive_failures, _last_pass_stats, _paused, _run_once_requested
    cfg = load_config()
    start_webhook_listener()
    signal.signal(signal.SIGTERM, _handle_sigterm)
    _consecutive_failures = 0
    while True:
        if _paused and not _run_once_requested:
            log("paused; waiting")
            WAKE_EVENT.wait(timeout=900)
            WAKE_EVENT.clear()
            continue
        try:
            stats = run_pass()
        except Exception as exc:
            halt_on_error(cfg, "pass", "run_pass", exc)
            stats = {"fetch_error": True}
        _last_pass_stats = stats
        _run_once_requested = False
        if _stop_requested:
            log("stop requested, exiting after pass")
            return 0
        if stats.get("fetch_error"):
            _consecutive_failures += 1
        else:
            _consecutive_failures = 0
        remaining = stats.get("wanted_after")
        if remaining is None:
            remaining = stats.get("wanted_before", 0)
        processed = stats.get("processed") or 0
        failed = stats.get("failed") or 0
        busy = (processed > 0 and (remaining or 0) > 0) or failed > 0
        if _consecutive_failures >= 3:
            delay = 900  # Bazarr/API down - stop hot-looping
        elif busy:
            delay = 60  # backlog or failures - re-check soon
        else:
            delay = 900  # idle
        log(
            f"sleeping {delay}s until next pass (busy={busy}, consecutive_failures={_consecutive_failures})"
        )
        WAKE_EVENT.wait(timeout=delay)
        WAKE_EVENT.clear()


if __name__ == "__main__":
    sys.exit(main())
