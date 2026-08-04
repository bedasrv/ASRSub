#!/usr/bin/env python3
"""Read-only dashboard for ASRSub (ASR Subtitles) (FastAPI + static HTML)."""

import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = "/home/user/.config/asr-pipeline/pipeline.env"
STATE_FILE = "/home/user/.config/asr-pipeline/state.jsonl"
HTML_FILE = os.path.join(BASE_DIR, "dashboard.html")

CACHE_TTL = 10.0
SERIES_CACHE_TTL = 60.0

app = FastAPI(title="ASRSub")


def load_cfg():
    cfg = {}
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    except Exception:
        pass
    return cfg


# ---------- caches ----------


def _cached(key, ttl, fn):
    now = time.time()
    c = getattr(_cached, "_store", None)
    if c is None:
        c = _cached._store = {}
    hit = c.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    c[key] = (now, val)
    return val


# ---------- data sources ----------


def read_state():
    entries = []
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        for line in lines[-100:]:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if isinstance(e, dict):
                    entries.append(e)
            except Exception:
                continue
    except Exception:
        pass
    latest = {}
    for e in entries:
        ep = e.get("sonarrEpisodeId")
        if ep is None:
            continue
        cur = latest.get(ep)
        if cur is None or (e.get("ts") or "") >= (cur.get("ts") or ""):
            latest[ep] = e
    return entries, latest


def fetch_wanted(cfg):
    def _fn():
        try:
            r = httpx.get(
                cfg["BAZARR_URL"].rstrip("/") + "/episodes/wanted",
                params={"start": 0, "length": 50},
                headers={"X-API-KEY": cfg["BAZARR_API_KEY"]},
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return {"total": 0, "data": []}

    return _cached("wanted", CACHE_TTL, _fn)


def fetch_series(cfg):
    def _fn():
        mapping = {}
        try:
            r = httpx.get(
                cfg["SONARR_URL"].rstrip("/") + "/series",
                headers={"X-Api-Key": cfg["SONARR_API_KEY"]},
                timeout=10,
            )
            r.raise_for_status()
            for s in r.json():
                sid = s.get("id")
                title = s.get("title")
                if sid is not None:
                    mapping[sid] = title or "?"
        except Exception:
            pass
        return mapping

    return _cached("series", SERIES_CACHE_TTL, _fn)


def pipeline_status():
    try:
        p = subprocess.run(
            ["pgrep", "-f", "orchestrator.py"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if p.returncode == 0:
            pids = [x for x in p.stdout.split() if x.strip()]
            return {"running": True, "pid": int(pids[0])}
    except Exception:
        pass
    return {"running": False, "pid": None}


def asr_status():
    cfg = load_cfg()
    url = cfg.get("ASR_URL", "http://127.0.0.1:9000").rstrip("/") + "/health"
    try:
        t0 = time.time()
        r = httpx.get(url, timeout=2)
        latency = int((time.time() - t0) * 1000)
        return {"up": True, "latency_ms": latency}
    except Exception:
        pass
    try:
        host, port = "127.0.0.1", 9000
        if "/" not in cfg.get("ASR_URL", ""):
            pass
        t0 = time.time()
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        latency = int((time.time() - t0) * 1000)
        return {"up": True, "latency_ms": latency}
    except Exception:
        return {"up": False, "latency_ms": None}


# ---------- aggregation ----------


def build_status():
    cfg = load_cfg()
    now = time.time()
    entries, latest = read_state()
    wanted_json = fetch_wanted(cfg)
    series_map = fetch_series(cfg)
    pipe = pipeline_status()
    asr = asr_status()

    wanted_total = wanted_json.get("total", 0)
    wanted_data = wanted_json.get("data", []) or []

    state_summary = {"done": 0, "error": 0, "running": 0, "new": 0}
    for ep in latest.values():
        st = ep.get("status")
        if st == "done":
            state_summary["done"] += 1
        elif st == "error":
            state_summary["error"] += 1
        else:
            state_summary["running"] += 1

    def episode_label(ep_num, season_num):
        if isinstance(season_num, int) and isinstance(ep_num, int):
            return f"S{season_num:02d}E{ep_num:02d}"
        return f"S{season_num}E{ep_num}"

    def wanted_episode_label(item):
        epnum = item.get("episode_number")
        if epnum and isinstance(epnum, str) and "x" in epnum:
            s, _, e = epnum.partition("x")
            try:
                return f"S{int(s):02d}E{int(e):02d}"
            except Exception:
                return epnum
        return episode_label(item.get("episodeNumber"), item.get("seasonNumber"))

    wanted = []
    seen_eps = set()
    for item in wanted_data:
        ep_id = item.get("sonarrEpisodeId")
        if ep_id in seen_eps:
            continue
        seen_eps.add(ep_id)
        series_title = item.get("seriesTitle") or (item.get("series") or {}).get(
            "title", "?"
        )
        ep_title = item.get("episodeTitle") or (item.get("episode") or {}).get(
            "title", ""
        )
        missing = sorted({m.get("code2") for m in item.get("missing_subtitles", [])})
        missing = [m for m in missing if m]
        state = latest.get(ep_id)
        now_dt = datetime.now(timezone.utc)
        if state is None:
            state_status = "new"
            elapsed_s = None
            ts = None
        else:
            ts = state.get("ts")
            elapsed_s = state.get("elapsed_s")
            raw_status = state.get("status", "new")
            try:
                state_ts = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                age_min = (now_dt - state_ts).total_seconds() / 60.0
            except Exception:
                age_min = 99999
            if pipe.get("running") and age_min <= 30:
                state_status = "running"
            else:
                state_status = raw_status if raw_status in ("done", "error") else "new"
        wanted.append(
            {
                "sonarrEpisodeId": ep_id,
                "series": series_title,
                "episode": wanted_episode_label(item),
                "title": ep_title,
                "missing": missing,
                "state_status": state_status,
                "elapsed_s": elapsed_s,
                "ts": ts,
            }
        )
        if state_status == "new":
            state_summary["new"] += 1

    recent = []
    seen_recent = set()
    for e in reversed(entries[-15:]):
        ep = e.get("sonarrEpisodeId")
        if ep in seen_recent:
            continue
        seen_recent.add(ep)
        label = None
        for item in wanted_data:
            if item.get("sonarrEpisodeId") == ep:
                label = f"{item.get('seriesTitle', '?')} {wanted_episode_label(item)}"
                break
        recent.append(
            {
                "sonarrEpisodeId": ep,
                "status": e.get("status", "?"),
                "elapsed_s": e.get("elapsed_s"),
                "ts": e.get("ts"),
                "label": label or f"ep {ep}",
            }
        )

    return {
        "wanted_total": wanted_total,
        "wanted": wanted,
        "state_summary": state_summary,
        "recent": recent,
        "pipeline": pipe,
        "asr": asr,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------- routes ----------


@app.get("/")
def index():
    return FileResponse(HTML_FILE)


@app.get("/api/status")
def api_status():
    return JSONResponse(build_status())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
