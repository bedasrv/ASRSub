#!/usr/bin/env python3
"""Subtitle refinement pass: review AI-translated subtitles against the Japanese
ASR source and apply targeted line-level fixes, overwriting the SRT in place.

Reviews in 60-line chunks with deepseek-v4-flash via the zen/go endpoint.
Serial (API safe concurrency = 1). Never touches orchestrator.py.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

import orchestrator as o

REVIEW_URL = "https://opencode.ai/zen/go/v1/chat/completions"
REVIEW_MODEL = "deepseek-v4-flash"
CHUNK_SIZE = 60
REFINE_STATE_FILE = "/home/user/.config/asr-pipeline/refine_state.jsonl"
ASR_LANG = "ja"

SYSTEM_PROMPT = (
    "You are a subtitle translation reviewer for an anime episode.\n"
    "The Japanese source line is ground truth. The current translation may contain "
    "mistakes. Fix ONLY: mistranslations, wrong character/term names, grammatical "
    "errors, awkward phrasing, or lines that do not match the Japanese meaning.\n"
    "Do NOT restyle lines that are already correct. Do NOT add or remove lines.\n"
    "Do NOT add commentary.\n"
    'Reply with corrected lines ONLY, one per line, format: "i. corrected text"\n'
    "where i is the original line number. If NO line needs correction, reply with\n"
    "exactly: NONE"
)


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}", flush=True)


def parse_reply(content, chunk_start, n_lines):
    """Parse reviewer reply into {global_line_index: text} or None if malformed."""
    if content is None:
        return None
    s = content.strip()
    if s == "NONE":
        return {}
    changes = {}
    for line in s.splitlines():
        m = re.match(r"^(\d+)\.\s*(.+)$", line.strip())
        if not m:
            return None
        local = int(m.group(1))
        if local < 1 or local > n_lines:
            return None
        global_idx = chunk_start + local - 1
        if global_idx in changes:
            return None
        changes[global_idx] = m.group(2).strip()
    return changes


def is_target_lang(texts, lang):
    sample = " ".join(texts[:30]) if isinstance(texts, list) else texts
    cjk = len(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff]", sample))
    return cjk == 0


def review_post(cfg, messages):
    """POST one review request. Returns (status_code_or_0, content_or_None)."""
    key = cfg.get("TRANSLATE_API_KEY", "")
    if not key:
        return 0, None
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": REVIEW_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "thinking": {"type": "enabled", "effort": "max"},
        "max_tokens": 16384,
    }
    for attempt in range(3):
        try:
            r = requests.post(REVIEW_URL, headers=headers, json=payload, timeout=400)
        except requests.RequestException as exc:
            log(f"    [review] network error (attempt {attempt + 1}): {exc}")
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                return r.status_code, r.json()["choices"][0]["message"]["content"]
            except Exception:
                return r.status_code, None
        if r.status_code == 404:
            log(f"    [review] HTTP 404 (attempt {attempt + 1}) - deferring chunk")
            return r.status_code, None
        if r.status_code >= 500 or r.status_code == 429:
            log(
                f"    [review] HTTP {r.status_code} (attempt {attempt + 1}): {r.text[:200]} - retrying once"
            )
            time.sleep(15)
            try:
                r2 = requests.post(
                    REVIEW_URL, headers=headers, json=payload, timeout=400
                )
            except requests.RequestException as exc:
                log(f"    [review] network error on 5xx/429 retry: {exc}")
                return r.status_code, None
            if r2.status_code == 200:
                try:
                    return r2.status_code, r2.json()["choices"][0]["message"]["content"]
                except Exception:
                    return r2.status_code, None
            if r2.status_code == 404:
                log(f"    [review] HTTP 404 on 5xx/429 retry - deferring chunk")
                return r2.status_code, None
            log(
                f"    [review] HTTP {r2.status_code} on 5xx/429 retry: {r2.text[:200]} - deferring chunk"
            )
            return r2.status_code, None
        log(
            f"    [review] HTTP {r.status_code} (attempt {attempt + 1}): {r.text[:200]}"
        )
        if attempt < 2:
            time.sleep(5 * (attempt + 1))
    return 0, None


def review_chunk(cfg, ja_lines, tr_lines, chunk_start):
    """Review one chunk. Returns (changes, retries). changes: {global_idx: text}."""
    user_lines = []
    for i, (j, t) in enumerate(zip(ja_lines, tr_lines), 1):
        user_lines.append(f"{i}. JA: {j} | TR: {t}")
    user = (
        "Review these lines. Japanese source (JA) vs current translation (TR):\n\n"
        + "\n".join(user_lines)
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    retries = 0
    code, content = review_post(cfg, messages)
    if content is None:
        if code and code >= 500:
            return None, retries
        if code == 404:
            return None, retries
        if code == 0:
            return None, retries
    changes = parse_reply(content, chunk_start, len(tr_lines))
    if changes is None:
        retries = 1
        reason = "malformed line format / invalid or duplicate index"
        messages.append({"role": "assistant", "content": content})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Your previous reply was malformed: {reason}. Reply again with the "
                    "exact required format."
                ),
            }
        )
        code2, content2 = review_post(cfg, messages)
        if content2 is not None:
            changes = parse_reply(content2, chunk_start, len(tr_lines))
        else:
            changes = None
    if changes is None:
        return None, retries
    return changes, retries


def find_translated_srt(video_path, lang):
    base, ext = os.path.splitext(video_path)
    cand = f"{base}.{lang}.srt"
    if os.path.isfile(cand):
        return cand
    d = os.path.dirname(video_path)
    matches = []
    try:
        for f in os.listdir(d):
            if f.endswith(f".{lang}.srt") and ".test." not in f and ".orig" not in f:
                matches.append(os.path.join(d, f))
    except OSError:
        return None
    if not matches:
        return None
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0]


def regenerate_asr(cfg, ep_id, video_path):
    streams = o.probe_audio(video_path)
    decision = o.choose_source(streams, "ja")
    if decision is None:
        raise RuntimeError("no audio streams")
    os.makedirs(cfg["TMP_DIR"], exist_ok=True)
    wav = os.path.join(cfg["TMP_DIR"], f"refine_{ep_id}.wav")
    try:
        o.extract_wav(video_path, decision["stream_index"], wav)
        cues = o.asr_cues(cfg, wav, decision["asr_lang"])
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    o.asr_cache_put(ep_id, decision["asr_lang"], video_path, cues)
    return cues, True


def get_asr_text(cfg, ep_id, video_path, no_regen):
    cues = o.asr_cache_get(ep_id, ASR_LANG, video_path)
    if cues is not None:
        return cues, False
    if no_regen:
        return None, False
    return regenerate_asr(cfg, ep_id, video_path)


def append_refine_state(entry):
    os.makedirs(os.path.dirname(REFINE_STATE_FILE), exist_ok=True)
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    with open(REFINE_STATE_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def process_episode(cfg, ep_id, langs, dry_run, no_regen):
    info = o.get_episode(cfg, ep_id)
    series_id = info.get("seriesId")
    season = info.get("seasonNumber", "?")
    ep_num = info.get("episodeNumber", "?")
    if isinstance(season, int) and isinstance(ep_num, int):
        tag = f"S{season:02d}E{ep_num:02d}"
    else:
        tag = f"S{season}E{ep_num}"
    series = "?"
    ef = info.get("episodeFile") or {}
    container_path = ef.get("path")
    if not ef or not container_path:
        log(f"skip: {tag} no episodeFile/path")
        append_refine_state(
            {
                "ep_id": ep_id,
                "series_id": series_id,
                "lang": ",".join(langs),
                "total_lines": 0,
                "changed": 0,
                "chunks": 0,
                "chunk_retries": 0,
                "regenerated_asr": False,
                "status": "skipped",
                "reason": "no episodeFile",
            }
        )
        return 0, "skipped"
    video_path = o.map_path(container_path)
    if not os.path.exists(video_path):
        log(f"skip: {tag} video not found: {video_path}")
        append_refine_state(
            {
                "ep_id": ep_id,
                "series_id": series_id,
                "lang": ",".join(langs),
                "total_lines": 0,
                "changed": 0,
                "chunks": 0,
                "chunk_retries": 0,
                "regenerated_asr": False,
                "status": "skipped",
                "reason": "video not found",
            }
        )
        return 0, "skipped"
    try:
        series_json = requests.get(
            cfg["SONARR_URL"].rstrip("/") + f"/series/{series_id}",
            headers={"X-Api-Key": cfg["SONARR_API_KEY"]},
            timeout=60,
        ).json()
        series = series_json.get("title", "?")
    except Exception:
        pass

    total_changed = 0
    for lang in langs:
        srt_path = find_translated_srt(video_path, lang)
        if not srt_path:
            log(f"skip: {tag} [{lang}] no .{lang}.srt found")
            append_refine_state(
                {
                    "ep_id": ep_id,
                    "series_id": series_id,
                    "lang": lang,
                    "total_lines": 0,
                    "changed": 0,
                    "chunks": 0,
                    "chunk_retries": 0,
                    "regenerated_asr": False,
                    "status": "skipped",
                    "reason": "no srt file",
                }
            )
            continue
        ja_cues, regen = get_asr_text(cfg, ep_id, video_path, no_regen)
        if ja_cues is None:
            log(f"skip: {tag} [{lang}] ASR not in cache and --no-regen set")
            append_refine_state(
                {
                    "ep_id": ep_id,
                    "series_id": series_id,
                    "lang": lang,
                    "total_lines": 0,
                    "changed": 0,
                    "chunks": 0,
                    "chunk_retries": 0,
                    "regenerated_asr": False,
                    "status": "skipped",
                    "reason": "no ASR source",
                }
            )
            continue
        tr_cues = o.parse_srt(
            open(srt_path, "r", encoding="utf-8", errors="replace").read()
        )
        had_marker = False
        if not any("AI-generated" in c["text"] for c in ja_cues):
            for i, c in enumerate(tr_cues):
                if "AI-generated" in c["text"]:
                    del tr_cues[i]
                    had_marker = True
                    break
        sample = " ".join(c["text"] for c in tr_cues[:30])
        if not is_target_lang(sample, lang):
            log(f"skip: {tag} [{lang}] srt is Japanese (echo), not a translation")
            append_refine_state(
                {
                    "ep_id": ep_id,
                    "series_id": series_id,
                    "lang": lang,
                    "total_lines": len(tr_cues),
                    "changed": 0,
                    "chunks": 0,
                    "chunk_retries": 0,
                    "regenerated_asr": regen,
                    "status": "skipped",
                    "reason": "srt is Japanese echo",
                }
            )
            continue
        if len(ja_cues) != len(tr_cues):
            log(
                f"skip: {tag} [{lang}] cue count mismatch ja={len(ja_cues)} tr={len(tr_cues)}"
            )
            append_refine_state(
                {
                    "ep_id": ep_id,
                    "series_id": series_id,
                    "lang": lang,
                    "total_lines": len(tr_cues),
                    "changed": 0,
                    "chunks": 0,
                    "chunk_retries": 0,
                    "regenerated_asr": regen,
                    "status": "skipped",
                    "reason": f"cue count mismatch ja={len(ja_cues)} tr={len(tr_cues)}",
                }
            )
            continue
        texts = [c["text"] for c in tr_cues]
        n = len(texts)
        chunks = 0
        retries = 0
        for start in range(0, n, CHUNK_SIZE):
            chunks += 1
            ja_chunk = [c["text"] for c in ja_cues[start : start + CHUNK_SIZE]]
            tr_chunk = texts[start : start + CHUNK_SIZE]
            changes, r = review_chunk(cfg, ja_chunk, tr_chunk, start)
            retries += r or 0
            if changes is None:
                log(
                    f"    [review] chunk {start + 1}-{start + len(tr_chunk)} deferred (unchanged)"
                )
                continue
            for idx, new_text in changes.items():
                texts[idx] = new_text
        changed = sum(1 for a, b in zip([c["text"] for c in tr_cues], texts) if a != b)
        total_changed += changed
        asr_src = "regen" if regen else "cache"
        log(
            f"refine: {series} {tag} [{lang}] lines={n} changed={changed} chunks={chunks} (asr={asr_src})"
        )
        if changed == 0:
            append_refine_state(
                {
                    "ep_id": ep_id,
                    "series_id": series_id,
                    "lang": lang,
                    "total_lines": n,
                    "changed": 0,
                    "chunks": chunks,
                    "chunk_retries": retries,
                    "regenerated_asr": regen,
                    "status": "done",
                    "reason": "",
                }
            )
            continue
        if not dry_run:
            tmp = srt_path + ".refine.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                o.write_srt(
                    tr_cues, texts, tmp, header=o.AI_MARKER if had_marker else None
                )
            os.replace(tmp, srt_path)
        append_refine_state(
            {
                "ep_id": ep_id,
                "series_id": series_id,
                "lang": lang,
                "total_lines": n,
                "changed": changed,
                "chunks": chunks,
                "chunk_retries": retries,
                "regenerated_asr": regen,
                "status": "done",
                "reason": "",
            }
        )
    return total_changed, "done"


def main(argv):
    ap = argparse.ArgumentParser(description="Subtitle refinement pass")
    ap.add_argument("--episodes", default="", help="comma list of sonarrEpisodeId")
    ap.add_argument(
        "--langs", default="", help="comma list of languages (default: all done)"
    )
    ap.add_argument("--limit", type=int, default=0, help="max episodes processed")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument(
        "--no-regen", action="store_true", help="skip episodes without cached ASR"
    )
    args = ap.parse_args(argv)

    cfg = o.load_config()
    target_langs = [x.strip() for x in args.langs.split(",") if x.strip()]

    episodes = {}
    if os.path.exists(o.STATE_FILE):
        with open(o.STATE_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("status") != "done":
                    continue
                ep_id = e.get("sonarrEpisodeId")
                lang = e.get("language")
                if ep_id is None or not lang:
                    continue
                episodes.setdefault(ep_id, set()).add(lang)
    if args.episodes:
        wanted = {int(x) for x in args.episodes.split(",") if x.strip()}
        episodes = {k: v for k, v in episodes.items() if k in wanted}
    if target_langs:
        episodes = {
            k: {l for l in v if l in target_langs} for k, v in episodes.items() if v
        }
    episodes = {k: sorted(v) for k, v in episodes.items()}
    ep_ids = sorted(episodes)
    if args.limit:
        ep_ids = ep_ids[: args.limit]

    n_episodes = 0
    n_changed = 0
    n_unchanged = 0
    t_changed = 0
    for ep_id in ep_ids:
        langs = episodes[ep_id]
        try:
            changed, status = process_episode(
                cfg, ep_id, langs, args.dry_run, args.no_regen
            )
            n_episodes += 1
            if status == "done" and changed:
                n_changed += 1
                t_changed += changed
            elif status == "skipped":
                n_unchanged += 1
            elif status == "done" and changed == 0:
                n_unchanged += 1
        except Exception as exc:
            import traceback

            log(f"fail: ep {ep_id} {exc}")
            traceback.print_exc()
            append_refine_state(
                {
                    "ep_id": ep_id,
                    "series_id": None,
                    "lang": ",".join(langs),
                    "total_lines": 0,
                    "changed": 0,
                    "chunks": 0,
                    "chunk_retries": 0,
                    "regenerated_asr": False,
                    "status": "failed",
                    "reason": str(exc)[:500],
                }
            )
            n_unchanged += 1
    log(
        f"refine pass: {n_episodes} episodes, {n_changed} changed, "
        f"{n_unchanged} unchanged/skipped, {t_changed} total lines changed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
