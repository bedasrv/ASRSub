#!/usr/bin/env python3
"""ASRSub control/telemetry API v2 - stdlib only.

Frozen public surface (cross-track contract with the rewrite track):

    class ControlAPIv2(cfg)                    # cfg: optional dict of option overrides
        .endpoints                             # dict: path -> handler(body, id=None) -> (code, obj)
        .register(server)                      # attach handlers to a ThreadingHTTPServer
        .handle(method, path, body=None, token=None)   # in-process dispatch

Endpoints (all JSON; errors as {"error": ...}):
    GET  /api2/status     pipeline state + GPU + llama-server + queue depth + per-episode progress
    GET  /api2/activity   state.jsonl tail + Bazarr history merged, newest first, episode titles
    GET  /api2/wanted     Bazarr wanted list with quality flags + missing langs + per-episode state
    GET  /api2/library    Bazarr wanted + state + exclusions merged, one item per episode
                          (sorted by series/season/episode, capped at 1000)
    GET  /api2/config     pipeline.env + config.overrides.json merged, secrets masked
    POST /api2/config     set/unset overrides (JSON body key=value; null unsets)
    GET  /api2/health     {"ok": true}
    GET  /api2/exclusions  list persistent exclusion records (exclusions.jsonl)
    POST /api2/episode/{id}/retry     append a retry record to actions.jsonl (see schema below)
    POST /api2/episode/{id}/skip      append a skip record to actions.jsonl
    POST /api2/episode/{id}/delete    append a delete record to actions.jsonl
    POST /api2/episode/{id}/exclude   add a persistent exclusion (exclusions.jsonl)
    POST /api2/episode/{id}/unexclude remove an exclusion
    POST /api2/pause | resume | run-once | wake   proxy to the v1 ControlHandler on :8085

Auth: POST endpoints require X-API-Key == CONTROL_API_KEY (same rule as the v1
ControlHandler in orchestrator.py). GET endpoints are read-only telemetry and
are left open so the dashboard browser page never holds the token. The dashboard
server proxies in-process and supplies the token itself.

actions.jsonl schema (runtime file, never committed):
    one JSON object per line, appended atomically:
    {"ts": "<ISO8601 UTC>", "type": "retry"|"skip"|"delete", "episode_id": <int>,
     "language": null|<lang code>, "source": "dashboard"|"pctl", "note": ""}
    episode_id corresponds to sonarrEpisodeId in state.jsonl.
    - retry:  re-process the episode for the listed language(s); language null = all missing.
    - skip:   treat as skipped; do not process it again.
    - delete: remove the episode's subtitle files from disk (see list_subtitle_files);
              language null = all languages.
    Consumers (rewrite track) read the tail, honor each record once, and may
    truncate the file once caught up.

exclusions.jsonl schema (runtime file, never committed):
    one JSON object per line, rewritten atomically on change:
    {"episode_id": <int>, "series_id": <int|null>, "reason": "<str>", "ts": "<ISO8601 UTC>"}
    Excluded episodes are never picked up by the pipeline again; the wanted list
    keeps showing them (EXCLUDED badge) so the exclusion can be undone later.
"""

import fcntl
import glob
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

CFG_DIR = os.path.join(os.path.expanduser("~"), ".config", "asr-pipeline")
ENV_FILE = os.path.join(CFG_DIR, "pipeline.env")
OVERRIDE_FILE = os.path.join(CFG_DIR, "config.overrides.json")
STATE_FILE = os.path.join(CFG_DIR, "state.jsonl")
REFINE_FILE = os.path.join(CFG_DIR, "refine_state.jsonl")
ACTIONS_FILE = os.path.join(CFG_DIR, "actions.jsonl")
EXCLUSIONS_FILE = os.path.join(CFG_DIR, "exclusions.jsonl")

SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
EP_LABEL_RE = re.compile(r"(\d+)x(\d+)")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(ts):
    if not ts:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%m/%d/%y %H:%M:%S",
    ):
        try:
            d = datetime.strptime(ts, fmt)
            if fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
                d = d.replace(tzinfo=timezone.utc)
            return d.timestamp()
        except ValueError:
            continue
    return None


def _http(method, url, headers=None, body=None, timeout=10):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw}
    except Exception as exc:
        return 0, {"error": str(exc)}


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class ControlAPIv2:
    DEFAULTS = {
        "ENV_FILE": ENV_FILE,
        "OVERRIDE_FILE": OVERRIDE_FILE,
        "STATE_FILE": STATE_FILE,
        "REFINE_FILE": REFINE_FILE,
        "ACTIONS_FILE": ACTIONS_FILE,
        "EXCLUSIONS_FILE": EXCLUSIONS_FILE,
        "NAS_MEDIA_ROOT": "/mnt/nas/share/media",
        "CONTROL_URL": "http://127.0.0.1:8085",
        "LLAMA_URL": "http://127.0.0.1:8011",
        "NVIDIA_SMI_CMD": [
            "nvidia-smi",
            "--query-gpu=name,memory.used,memory.total,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
    }

    def __init__(self, cfg=None):
        self.opts = dict(self.DEFAULTS)
        if cfg:
            self.opts.update(cfg)
        self._cache = {}
        self._excl_lock = threading.Lock()
        self._post_handlers = (
            self._h_retry,
            self._h_skip,
            self._h_delete,
            self._h_exclude,
            self._h_unexclude,
            self._h_pause,
            self._h_resume,
            self._h_run_once,
            self._h_wake,
        )
        self.endpoints = {
            "/api2/status": self._h_status,
            "/api2/activity": self._h_activity,
            "/api2/wanted": self._h_wanted,
            "/api2/library": self._h_library,
            "/api2/config": self._h_config,
            "/api2/health": self._h_health,
            "/api2/exclusions": self._h_exclusions,
            "/api2/episode/{id}/retry": self._h_retry,
            "/api2/episode/{id}/skip": self._h_skip,
            "/api2/episode/{id}/delete": self._h_delete,
            "/api2/episode/{id}/exclude": self._h_exclude,
            "/api2/episode/{id}/unexclude": self._h_unexclude,
            "/api2/pause": self._h_pause,
            "/api2/resume": self._h_resume,
            "/api2/run-once": self._h_run_once,
            "/api2/wake": self._h_wake,
        }

    # ---------- dispatch ----------

    def register(self, server):
        server.api2 = self
        server.RequestHandlerClass = _RequestHandler
        return server

    def control_token(self):
        return self._env().get("CONTROL_API_KEY", "")

    def handle(self, method, path, body=None, token=None):
        route, params = self._match(path)
        if route is None:
            return 404, {"error": "not found"}
        if route in self._post_handlers:
            if method != "POST":
                return 405, {"error": "method not allowed", "method": method, "path": path}
        elif route != self._h_config and method != "GET":
            return 405, {"error": "method not allowed", "method": method, "path": path}
        if method == "POST" and not self._check_token(token):
            return 401, {"error": "unauthorized"}
        try:
            if method == "POST" and path == "/api2/config":
                return self._h_config_set(body, **params)
            return route(body, **params)
        except ApiError as exc:
            return exc.code, {"error": exc.message}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}

    def _match(self, path):
        if path in self.endpoints:
            return self.endpoints[path], {}
        for route, handler in self.endpoints.items():
            if "{id}" in route:
                prefix, suffix = route.split("{id}", 1)
                if path.startswith(prefix) and path.endswith(suffix):
                    mid = path[len(prefix) : len(path) - len(suffix)]
                    if mid.isdigit():
                        return handler, {"id": int(mid)}
        return None, {}

    def _check_token(self, token):
        key = self._env().get("CONTROL_API_KEY", "")
        if not key:
            return False
        return token == key

    # ---------- data sources ----------

    def _cached(self, key, ttl, fn):
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._cache[key] = (now, val)
        return val

    def _env(self):
        cfg = {}
        try:
            with open(self.opts["ENV_FILE"], encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip()
        except OSError:
            pass
        ov = {}
        try:
            with open(self.opts["OVERRIDE_FILE"], encoding="utf-8") as fh:
                data = json.loads(fh.read())
            if isinstance(data, dict):
                ov = data
        except Exception:
            pass
        merged = dict(cfg)
        for k, v in ov.items():
            merged[str(k)] = str(v)
        return merged

    @staticmethod
    def _mask(cfg):
        return {
            k: ("***" if any(h in k.upper() for h in SECRET_HINTS) else v)
            for k, v in cfg.items()
        }

    def _state(self):
        entries = []
        try:
            with open(self.opts["STATE_FILE"], encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(e, dict):
                        entries.append(e)
        except OSError:
            pass
        latest = {}
        latest_by_lang = {}
        for e in entries:
            ep = e.get("sonarrEpisodeId")
            if ep is None:
                continue
            cur = latest.get(ep)
            cur_epoch = _parse_ts(cur.get("ts")) if cur else None
            new_epoch = _parse_ts(e.get("ts"))
            if cur is None or (new_epoch or -1) >= (cur_epoch or -1):
                latest[ep] = e
            lang = e.get("language")
            if lang is not None:
                key = (ep, lang)
                lcur = latest_by_lang.get(key)
                lcur_epoch = _parse_ts(lcur.get("ts")) if lcur else None
                if lcur is None or (new_epoch or -1) >= (lcur_epoch or -1):
                    latest_by_lang[key] = e
        return entries, latest, latest_by_lang

    def _refine_latest(self):
        latest = {}
        try:
            with open(self.opts["REFINE_FILE"], encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(e, dict) or e.get("ep_id") is None:
                        continue
                    cur = latest.get(e["ep_id"])
                    if cur is None or (e.get("ts") or "") >= (cur.get("ts") or ""):
                        latest[e["ep_id"]] = e
        except OSError:
            pass
        return latest

    @staticmethod
    def _state_counts(latest):
        counts = {"done": 0, "error": 0, "pending": 0, "total": len(latest)}
        for e in latest.values():
            st = e.get("status")
            if st in ("done", "error"):
                counts[st] += 1
            else:
                counts["pending"] += 1
        return counts

    def _daemon(self, method, path, timeout=5):
        cfg = self._env()
        token = cfg.get("CONTROL_API_KEY", "")
        headers = {"X-API-Key": token} if token else {}
        return _http(
            method, self.opts["CONTROL_URL"].rstrip("/") + path, headers=headers, timeout=timeout
        )

    def _daemon_status(self):
        code, data = self._daemon("GET", "/status", timeout=3)
        if code != 200 or not isinstance(data, dict):
            msg = data.get("error") if isinstance(data, dict) else "unexpected response"
            return {
                "reachable": False,
                "error": msg if code == 0 else f"daemon status {code}: {msg}",
                "paused": None,
                "uptime_s": None,
                "run_once_requested": None,
                "last_pass": None,
                "consecutive_failures": None,
                "started_at": None,
                "state_counts": None,
            }
        return {
            "reachable": True,
            "error": None,
            "paused": data.get("paused"),
            "uptime_s": data.get("uptime_s"),
            "run_once_requested": data.get("run_once_requested"),
            "last_pass": data.get("last_pass"),
            "consecutive_failures": data.get("consecutive_failures"),
            "started_at": data.get("started_at"),
            "state_counts": data.get("state_counts"),
        }

    def _gpu(self):
        def _fn():
            try:
                p = subprocess.run(
                    self.opts["NVIDIA_SMI_CMD"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except Exception as exc:
                return {"available": False, "error": str(exc)[:200]}
            if p.returncode != 0:
                return {
                    "available": False,
                    "error": (p.stderr or "").strip()[:200] or "nvidia-smi failed",
                }

            def num(v):
                try:
                    return int(float(v))
                except Exception:
                    return None

            gpus = []
            for line in p.stdout.strip().splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) < 4:
                    continue
                gpus.append(
                    {
                        "name": parts[0],
                        "memory_used_mb": num(parts[1]),
                        "memory_total_mb": num(parts[2]),
                        "util_pct": num(parts[3]),
                    }
                )
            return {"available": bool(gpus), "gpus": gpus}

        return self._cached("gpu", 5, _fn)

    def _llama(self):
        def _fn():
            base = self.opts["LLAMA_URL"].rstrip("/")
            model = None
            code, data = _http("GET", base + "/v1/models", timeout=3)
            if code == 200 and isinstance(data, dict):
                lst = data.get("data") or data.get("models") or []
                if lst:
                    ident = lst[0].get("id") or lst[0].get("model")
                    if ident:
                        model = os.path.basename(str(ident).rstrip("/")) or str(ident)
            t0 = time.time()
            code, data = _http("GET", base + "/health", timeout=3)
            up = code == 200
            return {
                "up": up,
                "model": model,
                "latency_ms": int((time.time() - t0) * 1000) if up else None,
                "error": None if up else (data.get("error") if isinstance(data, dict) else None),
            }

        return self._cached("llama", 5, _fn)

    def _bazarr_wanted(self):
        def _fn():
            cfg = self._env()
            url_base = cfg.get("BAZARR_URL", "").rstrip("/")
            if not url_base:
                return {"total": 0, "data": []}
            url = url_base + "/episodes/wanted?start=0&length=60"
            code, data = _http(
                "GET", url, headers={"X-API-KEY": cfg.get("BAZARR_API_KEY", "")}, timeout=10
            )
            if code != 200 or not isinstance(data, dict):
                return {"total": 0, "data": []}
            return data

        return self._cached("bazarr.wanted", 10, _fn)

    def _bazarr_history(self):
        def _fn():
            cfg = self._env()
            url_base = cfg.get("BAZARR_URL", "").rstrip("/")
            if not url_base:
                return []
            url = url_base + "/episodes/history?start=0&length=40"
            code, data = _http(
                "GET", url, headers={"X-API-KEY": cfg.get("BAZARR_API_KEY", "")}, timeout=10
            )
            if code != 200 or not isinstance(data, dict):
                return []
            out = []
            for h in data.get("data", []) or []:
                out.append(
                    {
                        "ts": h.get("parsed_timestamp"),
                        "kind": "bazarr",
                        "episode_id": h.get("sonarrEpisodeId"),
                        "series": h.get("seriesTitle"),
                        "episode": self._episode_number_label(h.get("episode_number")),
                        "title": h.get("episodeTitle"),
                        "detail": h.get("description"),
                        "language": (h.get("language") or {}).get("code2"),
                        "elapsed_s": None,
                        "ai": bool(
                            h.get("subtitles_path")
                            and ".ai.srt" in str(h.get("subtitles_path"))
                        ),
                        "relative": h.get("timestamp"),
                    }
                )
            return out

        return self._cached("bazarr.history", 10, _fn)

    def _sonarr_series(self):
        def _fn():
            cfg = self._env()
            url_base = cfg.get("SONARR_URL", "").rstrip("/")
            if not url_base:
                return {}
            url = url_base + "/series"
            code, data = _http(
                "GET", url, headers={"X-Api-Key": cfg.get("SONARR_API_KEY", "")}, timeout=10
            )
            if code != 200 or not isinstance(data, list):
                return {}
            return {s.get("id"): s.get("title") or "?" for s in data if isinstance(s, dict)}

        return self._cached("sonarr.series", 300, _fn)

    def _ep_detail(self, ep_id):
        cached = self._cache.get(("ep", ep_id))
        now = time.time()
        if cached and now - cached[0] < 300:
            return cached[1]
        det = {
            "series": None,
            "episode": None,
            "title": None,
            "series_id": None,
            "season_number": None,
            "episode_number": None,
            "quality": None,
            "media": None,
        }
        cfg = self._env()
        url_base = cfg.get("SONARR_URL", "").rstrip("/")
        if not url_base:
            self._cache[("ep", ep_id)] = (now, det)
            return det
        url = url_base + f"/episode/{ep_id}?format=json"
        code, data = _http(
            "GET", url, headers={"X-Api-Key": cfg.get("SONARR_API_KEY", "")}, timeout=10
        )
        if code == 200 and isinstance(data, dict):
            ef = data.get("episodeFile") or {}
            q = (ef.get("quality") or {}).get("quality") or {}
            mi = ef.get("mediaInfo") or {}
            det = {
                "series": self._sonarr_series().get(data.get("seriesId")),
                "episode": self._episode_label(
                    data.get("seasonNumber"), data.get("episodeNumber")
                ),
                "title": data.get("title"),
                "series_id": data.get("seriesId"),
                "season_number": data.get("seasonNumber"),
                "episode_number": data.get("episodeNumber"),
                "path": ef.get("path"),
                "quality": {"resolution": q.get("resolution"), "source": q.get("name")},
                "media": {
                    "video_codec": mi.get("videoCodec"),
                    "audio_codec": mi.get("audioCodec"),
                    "resolution": mi.get("resolution"),
                },
            }
        self._cache[("ep", ep_id)] = (now, det)
        return det

    def list_subtitle_files(self, episode):
        """Resolve SRT/ASS subtitle files on disk for an episode.

        episode: a Sonarr episode dict (with episodeFile.path) or a detail dict
        from _ep_detail. Sonarr container paths under /data/... are mapped onto
        the NAS mount NAS_MEDIA_ROOT; the mapped directory wins when it exists,
        otherwise the raw path is tried. Returns a sorted list of absolute
        .srt/.ass file paths (empty when the episode has no files or the mount
        is not visible from this host).
        """
        path = None
        if isinstance(episode, dict):
            ef = episode.get("episodeFile") or {}
            path = episode.get("path") or ef.get("path")
        if not path:
            return []
        root = self.opts.get("NAS_MEDIA_ROOT", "/mnt/nas/share/media").rstrip("/")
        candidates = []
        if isinstance(path, str) and path.startswith("/data/"):
            candidates.append(root + path[len("/data"):])
        candidates.append(path)
        for base in candidates:
            if not isinstance(base, str) or not base:
                continue
            d = os.path.dirname(base)
            if os.path.isdir(d):
                files = glob.glob(os.path.join(d, "*.srt")) + glob.glob(
                    os.path.join(d, "*.ass")
                )
                return sorted(
                    f
                    for f in files
                    if ".test." not in os.path.basename(f)
                    and ".orig" not in os.path.basename(f)
                )
        return []

    @staticmethod
    def _episode_label(season_num, ep_num):
        if isinstance(season_num, int) and isinstance(ep_num, int):
            return f"S{season_num:02d}E{ep_num:02d}"
        return f"S{season_num}E{ep_num}"

    @staticmethod
    def _episode_number_label(epnum):
        if epnum:
            m = EP_LABEL_RE.search(str(epnum))
            if m:
                try:
                    return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
                except Exception:
                    return str(epnum)
        return str(epnum) if epnum else None

    def _quality_flags(self, det):
        flags = {"resolution": None, "codec": None, "audio": None, "source": None}
        if not det:
            return flags
        q = det.get("quality") or {}
        m = det.get("media") or {}
        if isinstance(q.get("resolution"), int):
            flags["resolution"] = f"{q['resolution']}p"
        else:
            res = m.get("resolution")
            if res and "x" in str(res):
                flags["resolution"] = str(res).split("x")[-1] + "p"
        flags["codec"] = m.get("video_codec")
        flags["audio"] = m.get("audio_codec")
        flags["source"] = q.get("source")
        return flags

    def _subs_cached(self, ep_id, det):
        return self._cached(
            ("subs", ep_id), 15, lambda: self.list_subtitle_files(det)
        )

    def _resolve_label(self, ep_id, wanted_data):
        for item in wanted_data:
            if item.get("sonarrEpisodeId") == ep_id:
                return (
                    item.get("seriesTitle"),
                    self._episode_number_label(item.get("episode_number")),
                    item.get("episodeTitle"),
                )
        det = self._ep_detail(ep_id)
        series, episode, title = det.get("series"), det.get("episode"), det.get("title")
        if not series and not episode:
            return None, f"ep {ep_id}", title
        return series, episode, title

    # ---------- endpoints ----------

    def _h_health(self, body, id=None):
        return 200, {"ok": True}

    def _h_config(self, body, id=None):
        return 200, self._mask(self._env())

    def _read_overrides(self):
        try:
            with open(self.opts["OVERRIDE_FILE"], encoding="utf-8") as fh:
                data = json.loads(fh.read())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_overrides(self, ov):
        path = self.opts["OVERRIDE_FILE"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(ov, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)

    def _h_config_set(self, body, id=None):
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        ov = self._read_overrides()
        applied = {}
        for k, v in body.items():
            if v is None:
                ov.pop(str(k), None)
                applied[str(k)] = None
            else:
                ov[str(k)] = str(v)
                applied[str(k)] = str(v)
        self._write_overrides(ov)
        return 200, {"ok": True, "applied": self._mask(applied)}

    def _h_status(self, body, id=None):
        entries, latest, _lbl = self._state()
        refine = self._refine_latest()
        daemon = self._daemon_status()
        wanted = self._bazarr_wanted()
        gpu = self._gpu()
        llama = self._llama()
        episodes = {}
        for ep_id, st in latest.items():
            rf = refine.get(ep_id)
            episodes[str(ep_id)] = {
                "status": st.get("status"),
                "language": st.get("language"),
                "elapsed_s": st.get("elapsed_s"),
                "ts": st.get("ts"),
                "refine": (
                    {"status": rf.get("status"), "ts": rf.get("ts")} if rf else None
                ),
            }
        counts = self._state_counts(latest)
        if daemon.get("reachable") and isinstance(daemon.get("state_counts"), dict):
            counts = daemon["state_counts"]
        return 200, {
            "updated_at": _now_iso(),
            "daemon": daemon,
            "state_counts": counts,
            "queue": {"wanted": wanted.get("total", 0)},
            "gpu": gpu,
            "llama": llama,
            "episodes": episodes,
        }

    def _h_activity(self, body, id=None):
        entries, _latest, _lbl = self._state()
        wanted = self._bazarr_wanted()
        items = []
        for e in entries[-40:]:
            ep_id = e.get("sonarrEpisodeId")
            series, episode, title = self._resolve_label(ep_id, wanted.get("data", []) or [])
            items.append(
                {
                    "ts": e.get("ts"),
                    "kind": "pipeline",
                    "episode_id": ep_id,
                    "series": series,
                    "episode": episode,
                    "title": title,
                    "detail": e.get("status"),
                    "language": e.get("language"),
                    "elapsed_s": e.get("elapsed_s"),
                    "ai": None,
                    "relative": None,
                }
            )
        items.extend(self._bazarr_history())
        items.sort(
            key=lambda it: (_parse_ts(it["ts"]) if _parse_ts(it["ts"]) is not None else -1),
            reverse=True,
        )
        seen = set()
        for it in items:
            if it.get("kind") == "pipeline" and it.get("episode_id") is not None:
                ts = _parse_ts(it.get("ts"))
                if ts is not None:
                    seen.add((it["episode_id"], int(ts // 300)))
        deduped = []
        for it in items:
            key = None
            if it.get("kind") != "pipeline" and it.get("episode_id") is not None:
                ts = _parse_ts(it.get("ts"))
                if ts is not None:
                    key = (it["episode_id"], int(ts // 300))
            if it.get("kind") == "pipeline" or key is None or key not in seen:
                deduped.append(it)
        return 200, {"items": deduped[:50], "updated_at": _now_iso()}

    def _h_wanted(self, body, id=None):
        wanted_json = self._bazarr_wanted()
        _entries, latest, latest_by_lang = self._state()
        refine = self._refine_latest()
        daemon = self._daemon_status()
        data = wanted_json.get("data", []) or []
        ep_ids = [it.get("sonarrEpisodeId") for it in data if it.get("sonarrEpisodeId") is not None]
        details = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            for eid, det in zip(ep_ids, ex.map(self._ep_detail, ep_ids)):
                details[eid] = det
        now = time.time()
        items = []
        seen = set()
        for it in data:
            ep_id = it.get("sonarrEpisodeId")
            if ep_id is None or ep_id in seen:
                continue
            seen.add(ep_id)
            missing = sorted(
                {
                    m.get("code2")
                    for m in (it.get("missing_subtitles") or [])
                    if isinstance(m, dict) and m.get("code2")
                }
            )
            st = latest.get(ep_id)
            state = {"status": "new", "elapsed_s": None, "ts": None}
            if st is not None:
                status = st.get("status", "new")
                if daemon.get("reachable") and status not in ("done", "error"):
                    ep_epoch = _parse_ts(st.get("ts"))
                    if ep_epoch is not None and now - ep_epoch <= 1800:
                        status = "running"
                state = {
                    "status": status,
                    "elapsed_s": st.get("elapsed_s"),
                    "ts": st.get("ts"),
                }
            lang_states = {}
            for lang, lst in ((l, r) for (e, l), r in latest_by_lang.items() if e == ep_id):
                lstatus = lst.get("status", "new")
                if daemon.get("reachable") and lstatus not in ("done", "error"):
                    l_epoch = _parse_ts(lst.get("ts"))
                    if l_epoch is not None and now - l_epoch <= 1800:
                        lstatus = "running"
                lang_states[lang] = {
                    "status": lstatus,
                    "elapsed_s": lst.get("elapsed_s"),
                    "ts": lst.get("ts"),
                }
            rf = refine.get(ep_id)
            items.append(
                {
                    "sonarrEpisodeId": ep_id,
                    "series_id": it.get("sonarrSeriesId"),
                    "series": it.get("seriesTitle"),
                    "episode": self._episode_number_label(it.get("episode_number")),
                    "title": it.get("episodeTitle"),
                    "missing": missing,
                    "quality": self._quality_flags(details.get(ep_id)),
                    "subtitle_files": self._subs_cached(ep_id, details.get(ep_id)),
                    "state": state,
                    "lang_states": lang_states,
                    "refine": (
                        {"status": rf.get("status"), "ts": rf.get("ts")} if rf else None
                    ),
                }
            )
        exclusions = {str(r.get("episode_id")): r for r in self._read_exclusions()}
        return 200, {
            "total": wanted_json.get("total", len(items)),
            "items": items,
            "exclusions": exclusions,
            "updated_at": _now_iso(),
        }

    def _h_library(self, body, id=None):
        """Merged library: one item per episode across Bazarr wanted + state
        history + exclusions, with per-language status. Read-only."""
        wanted_json = self._bazarr_wanted()
        _entries, latest, latest_by_lang = self._state()
        wanted_data = wanted_json.get("data", []) or []
        wanted_by_id = {}
        for it in wanted_data:
            eid = it.get("sonarrEpisodeId")
            if isinstance(eid, int) and eid not in wanted_by_id:
                wanted_by_id[eid] = it
        exclusions = self._read_exclusions()
        excluded_ids = {
            r.get("episode_id")
            for r in exclusions
            if isinstance(r.get("episode_id"), int)
        }
        ep_ids = {eid for eid in (set(wanted_by_id) | set(latest) | excluded_ids) if isinstance(eid, int)}
        detail_ids = [eid for eid in ep_ids if eid not in wanted_by_id]
        details = {}
        if detail_ids:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for eid, det in zip(detail_ids, ex.map(self._ep_detail, detail_ids)):
                    details[eid] = det
        langs = {}
        for (eid, lang), rec in latest_by_lang.items():
            langs.setdefault(eid, {})[lang] = {
                "status": rec.get("status"),
                "ts": rec.get("ts"),
                "elapsed_s": rec.get("elapsed_s"),
            }
        for eid, w in wanted_by_id.items():
            entry = langs.setdefault(eid, {})
            for m in (w.get("missing_subtitles") or []):
                if not isinstance(m, dict):
                    continue
                code = m.get("code2")
                if code and code not in entry:
                    entry[code] = {"status": "wanted", "ts": None, "elapsed_s": None}
        for eid in excluded_ids:
            for lang in langs.get(eid, {}):
                langs[eid][lang]["status"] = "excluded"
        items = []
        for eid in ep_ids:
            w = wanted_by_id.get(eid)
            if w is not None:
                series = w.get("seriesTitle")
                episode = self._episode_number_label(w.get("episode_number"))
                title = w.get("episodeTitle")
                season, epnum = w.get("seasonNumber"), w.get("episodeNumber")
                if not isinstance(season, int) or not isinstance(epnum, int):
                    m = EP_LABEL_RE.search(str(w.get("episode_number") or ""))
                    if m:
                        try:
                            season, epnum = int(m.group(1)), int(m.group(2))
                        except ValueError:
                            pass
            else:
                det = details.get(eid) or {}
                series = det.get("series")
                episode = det.get("episode")
                title = det.get("title")
                season = det.get("season_number")
                epnum = det.get("episode_number")
            if w is None and not series:
                continue  # ghost: episode deleted from Sonarr, not wanted
            items.append(
                {
                    "sonarr_episode_id": eid,
                    "series": series,
                    "episode": episode,
                    "title": title,
                    "season": season,
                    "episode_number": epnum,
                    "wanted": eid in wanted_by_id,
                    "languages": [
                        {
                            "language": lang,
                            "status": st["status"],
                            "ts": st["ts"],
                            "elapsed_s": st["elapsed_s"],
                        }
                        for lang, st in sorted(langs.get(eid, {}).items())
                    ],
                }
            )
        items.sort(
            key=lambda it: (
                it["series"] or "",
                it["season"] if isinstance(it["season"], int) else -1,
                it["episode_number"] if isinstance(it["episode_number"], int) else -1,
            )
        )
        out = {"items": items[:1000]}
        if len(items) > 1000:
            out["truncated"] = True
        return 200, out

    def _append_action(self, kind, ep_id, body):
        lang = None
        if isinstance(body, dict) and isinstance(body.get("language"), str):
            lang = body["language"]
        rec = {
            "ts": _now_iso(),
            "type": kind,
            "episode_id": ep_id,
            "language": lang,
            "source": "dashboard",
            "note": "",
        }
        path = self.opts["ACTIONS_FILE"]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise ApiError(500, f"failed to write actions.jsonl: {exc}")
        return 200, {"ok": True, "record": rec}

    def _h_retry(self, body, id=None):
        return self._append_action("retry", id, body)

    def _h_skip(self, body, id=None):
        return self._append_action("skip", id, body)

    def _h_delete(self, body, id=None):
        return self._append_action("delete", id, body)

    def _read_exclusions(self):
        recs = []
        try:
            with open(self.opts["EXCLUSIONS_FILE"], encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(e, dict) and e.get("episode_id") is not None:
                        recs.append(e)
        except OSError:
            pass
        return recs

    def _write_exclusions(self, recs):
        path = self.opts["EXCLUSIONS_FILE"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    def _ep_series_id(self, ep_id, wanted_data):
        for it in wanted_data:
            if it.get("sonarrEpisodeId") == ep_id:
                return it.get("sonarrSeriesId")
        return None

    def _h_exclusions(self, body, id=None):
        recs = self._read_exclusions()
        return 200, {"total": len(recs), "exclusions": recs, "updated_at": _now_iso()}

    def _h_exclude(self, body, id=None):
        reason = ""
        if isinstance(body, dict) and isinstance(body.get("reason"), str):
            reason = body["reason"]
        wanted = self._bazarr_wanted()
        series_id = self._ep_series_id(id, wanted.get("data", []) or [])
        if series_id is None:
            series_id = self._ep_detail(id).get("series_id")
        rec = {
            "episode_id": id,
            "series_id": series_id,
            "reason": reason,
            "ts": _now_iso(),
        }
        with self._excl_lock:
            for r in self._read_exclusions():
                if r.get("episode_id") == id:
                    return 200, {"ok": True, "already_excluded": True, "record": r}
            try:
                recs = self._read_exclusions()
                recs.append(rec)
                self._write_exclusions(recs)
            except OSError as exc:
                raise ApiError(500, f"failed to write exclusions.jsonl: {exc}")
        return 200, {"ok": True, "record": rec}

    def _h_unexclude(self, body, id=None):
        with self._excl_lock:
            recs = self._read_exclusions()
            before = len(recs)
            recs = [r for r in recs if r.get("episode_id") != id]
            try:
                self._write_exclusions(recs)
            except OSError as exc:
                raise ApiError(500, f"failed to write exclusions.jsonl: {exc}")
        return 200, {"ok": True, "removed": before - len(recs)}

    def _daemon_action(self, path):
        code, data = self._daemon("POST", path)
        if code == 0:
            raise ApiError(
                503, f"control daemon unreachable: {data.get('error', 'connection failed')}"
            )
        return code, data

    def _h_pause(self, body, id=None):
        return self._daemon_action("/pause")

    def _h_resume(self, body, id=None):
        return self._daemon_action("/resume")

    def _h_run_once(self, body, id=None):
        return self._daemon_action("/run-once")

    def _h_wake(self, body, id=None):
        return self._daemon_action("/wake")


class _RequestHandler(BaseHTTPRequestHandler):
    def _handle(self, method):
        api = getattr(self.server, "api2", None)
        if api is None:
            self._send(500, {"error": "api2 not registered"})
            return
        path = self.path.split("?", 1)[0]
        body = None
        if method == "POST":
            raw = b""
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 0:
                    raw = self.rfile.read(length)
            except Exception:
                pass
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._send(400, {"error": "invalid JSON body"})
                return
        token = self.headers.get("X-API-Key")
        code, obj = api.handle(method, path, body, token=token)
        self._send(code, obj)

    def _send(self, code, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    import sys

    from http.server import ThreadingHTTPServer

    port = int(os.environ.get("API2_PORT") or (sys.argv[1] if len(sys.argv) > 1 else "8086"))
    server = ThreadingHTTPServer(("127.0.0.1", port), _RequestHandler)
    ControlAPIv2().register(server)
    print(f"api2 listening on 127.0.0.1:{port} (GET /api2/status, /api2/health)", flush=True)
    server.serve_forever()
