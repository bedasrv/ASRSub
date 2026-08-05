#!/usr/bin/env python3
"""AI subtitle pipeline orchestrator: wanted list -> ASR -> LLM translate -> Bazarr upload.

Self-looping daemon (systemd Restart=always; flock guards single instance). Idempotent via JSONL state file.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOG_LOCK = threading.Lock()

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
MODEL_FALLBACKS = []
MAX_WORKERS = int(os.environ.get("MAX_TRANSLATE_WORKERS", "1"))
WAKE_EVENT = threading.Event()
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8085"))


def load_config():
    cfg = {}
    with open(ENV_FILE, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
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


def is_done(entries, ep_id, lang):
    for e in entries:
        if (
            e.get("sonarrEpisodeId") == ep_id
            and e.get("language") == lang
            and e.get("status") == "done"
        ):
            return True
    return False


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


def post_chat(cfg, messages, model, key):
    url = cfg["TRANSLATE_BASE"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
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
            if len(lines) >= 200:
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
                        if len(lines) >= 200:
                            break
            except Exception:
                continue
        return lines[:200]
    except Exception:
        return []


def chat_translate_batch(cfg, lines, target_lang, key, context_lines=None):
    models = [cfg["TRANSLATE_MODEL"]] + [
        m for m in MODEL_FALLBACKS if m != cfg["TRANSLATE_MODEL"]
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


def translate_texts(cfg, cues, target_lang, key, series_id=None, episode_id=None):
    lines = [c["text"] for c in cues]
    context_lines = []
    if series_id and episode_id:
        context_lines = get_prior_context(cfg, series_id, episode_id)
    out = chat_translate_batch(cfg, lines, target_lang, key, context_lines)
    if out is None or len(out) != len(lines):
        return None
    return out


# ---------- upload ----------


def upload_srt(cfg, series_id, ep_id, lang, srt_bytes):
    r = requests.post(
        cfg["BAZARR_URL"].rstrip("/") + "/episodes/subtitles",
        params={
            "seriesid": series_id,
            "episodeid": ep_id,
            "language": lang,
            "forced": "false",
            "hi": "false",
        },
        headers={"X-API-KEY": cfg["BAZARR_API_KEY"]},
        files={"file": ("sub.srt", srt_bytes, "application/x-subrip")},
        timeout=120,
    )
    return r.status_code


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
):
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
        return "failed"


# ---------- main ----------


def run_pass():
    cfg = load_config()
    target_langs = set(cfg["TARGET_LANGS"])
    key = cfg.get("TRANSLATE_API_KEY", "")
    if not key:
        log(
            "WARNING: TRANSLATE_API_KEY is empty; any episode needing translation will FAIL"
        )

    entries = load_state()

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

    futures = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for item in candidates:
            ep_id = item["sonarrEpisodeId"]
            series = item.get("seriesTitle", "?")
            missing = {m.get("code2") for m in item.get("missing_subtitles", [])}
            langs = sorted(missing & target_langs)
            for lang in langs:
                if is_done(entries, ep_id, lang):
                    log(f"skip: S?E? {series} [{lang}] already done (state)")
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
                    extract_wav(media_path, decision["stream_index"], wav_path)

                    t_asr = time.time()
                    srt_text = asr_srt(cfg, wav_path, decision["asr_lang"])
                    asr_elapsed = time.time() - t_asr
                    cues = parse_srt(srt_text)
                    if not cues:
                        raise RuntimeError("ASR returned no cues")
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

        for fut in as_completed(futures):
            if fut.result() == "done":
                done += 1
            else:
                failed += 1

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


class _WakeHandler(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()
        WAKE_EVENT.set()

    def do_GET(self):
        self._ok()

    def do_POST(self):
        self._ok()

    def log_message(self, *args):
        pass


def start_webhook_listener():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", WEBHOOK_PORT), _WakeHandler)
    except OSError as exc:
        log(
            f"webhook listener unavailable on :{WEBHOOK_PORT} ({exc}); continuing without it"
        )
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"webhook listener on :{WEBHOOK_PORT} (any request wakes the loop)")


def main():
    lock_path = "/home/user/.config/asr-pipeline/orchestrator.lock"
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another orchestrator instance running, exiting")
        return 0
    start_webhook_listener()
    consecutive_failures = 0
    while True:
        try:
            stats = run_pass()
        except Exception as exc:
            log(f"pass crashed: {exc}")
            stats = {"fetch_error": True}
        if stats.get("fetch_error"):
            consecutive_failures += 1
        else:
            consecutive_failures = 0
        remaining = stats.get("wanted_after")
        if remaining is None:
            remaining = stats.get("wanted_before", 0)
        busy = (remaining or 0) > 0 or (stats.get("failed") or 0) > 0
        if consecutive_failures >= 3:
            delay = 900  # Bazarr/API down - stop hot-looping
        elif busy:
            delay = 60  # backlog or failures - re-check soon
        else:
            delay = 900  # idle
        log(
            f"sleeping {delay}s until next pass (busy={busy}, consecutive_failures={consecutive_failures})"
        )
        WAKE_EVENT.wait(timeout=delay)
        WAKE_EVENT.clear()


if __name__ == "__main__":
    sys.exit(main())
