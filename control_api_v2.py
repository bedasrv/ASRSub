#!/usr/bin/env python3
"""ASRSub control/telemetry API v2 - stdlib only.

Frozen public surface (cross-track contract with the rewrite track):

    class ControlAPIv2(cfg)                    # cfg: optional dict of option overrides
        .endpoints                             # dict: path -> handler(body, id=None) -> (code, obj)
        .register(server)                      # attach handlers to a ThreadingHTTPServer
        .handle(method, path, body=None, token=None)   # in-process dispatch

Endpoints (all JSON; errors as {"error": ...}):
    GET  /api2/status     pipeline state + GPU + llama-server + queue depth + per-episode progress + registry summary
    GET  /api2/provenance subtitle_registry.jsonl totals (embedded/external), per-language breakdown, recent rows
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

retry/delete are validated against Sonarr BEFORE enqueueing (validate_retry_target):
a ghost episode (Sonarr 404), an episode without a video file, or an unmonitored
episode is rejected with 409 {"ok": false, "error": <reason>} and NOT enqueued —
an unvalidated action would be consumed by the daemon as a silent no-op (Bazarr
only wants monitored episodes WITH files). skip/exclude/unexclude are not
validated.

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
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

CFG_DIR = os.path.join(os.path.expanduser("~"), ".config", "asr-pipeline")
ENV_FILE = os.path.join(CFG_DIR, "pipeline.env")
OVERRIDE_FILE = os.path.join(CFG_DIR, "config.overrides.json")
STATE_FILE = os.path.join(CFG_DIR, "state.jsonl")
REGISTRY_FILE = os.path.join(CFG_DIR, "subtitle_registry.jsonl")
REFINE_FILE = os.path.join(CFG_DIR, "refine_state.jsonl")
ACTIONS_FILE = os.path.join(CFG_DIR, "actions.jsonl")
EXCLUSIONS_FILE = os.path.join(CFG_DIR, "exclusions.jsonl")

SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "AUTH", "CRED")
EP_LABEL_RE = re.compile(r"(\d+)x(\d+)")
# ISO 639-2 -> ISO 639-1 for registry/state language codes shown in the UI
# (eng/jpn/ind merge into en/ja/id so provenance rows don't duplicate).
LANG_NORM = {"eng": "en", "jpn": "ja", "ind": "id", "en": "en", "ja": "ja", "id": "id"}

# Code-default values for keys that only exist as orchestrator module defaults
# (not in pipeline.env / config.overrides.json). Mirrors orchestrator.py. Used
# so GET /api2/config exposes every key the Settings UI renders.
CONFIG_DEFAULTS = {
    "ALIGN_ENABLED": "true",
    "ALIGN_MAX_OFFSET_S": "1.5",
    "ALIGN_MAX_OFFSET_SECONDS": "60",
    "RETIME_ENABLED": "true",
    "RETIME_TEXT_THRESHOLD": "0.55",
    "RETIME_MIN_ANCHOR_FRAC": "0.25",
    "RETIME_MAX_RATIO": "3.0",
    "CPS_MERGE_MAX": "20",
    "CPS_MERGE_MAX_CHARS": "84",
    "CPS_MERGE_MAX_DUR_MS": "7000",
    "CPS_MERGE_MAX_GAP_MS": "1000",
    "LADDER_COOLDOWN_H": "24",
    "LADDER_MIN_CHARS": "1500",
    "LADDER_MIN_CJK": "0.6",
    "LADDER_MIN_CUES": "40",
    "LADDER_SPAN_TOLERANCE": "0.15",
    "LADDER_UPGRADE_BUDGET": "4",
    "LADDER_SKIP_REFINED": "true",
    "TRANSLATE_CHUNK": "10",
    "TRANSLATE_CONTEXT_LINES": "2",
    "TRANSLATE_FALLBACK_MODELS": "",
    "MAX_TRANSLATE_WORKERS": "1",
    "AI_MARKER_CUE": "1",
    "AI_MARKER_CUE_MS": "1500",
    "SDH_PLACEHOLDERS": '["（歌詞）"]',
    "WEBHOOK_URLS": "",
    "WEBHOOK_SECRET": "",
    "WEBHOOK_EVENTS": "",
    "HERMES_WEBHOOK_URL": "",
    "HERMES_WEBHOOK_SECRET": "",
    "WEBHOOK_PORT": "8085",
    "STATE_FILE": STATE_FILE,
    "ACTIONS_FILE": ACTIONS_FILE,
    "EXCLUSIONS_FILE": EXCLUSIONS_FILE,
    "REGISTRY_FILE": REGISTRY_FILE,
    "REFINE_STATE_FILE": REFINE_FILE,
    "ASR_CACHE_DIR": os.path.join(
        os.path.expanduser("~"), ".cache", "asr-pipeline", "asr"
    ),
}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(ts):
    if not ts:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%m/%d/%y %H:%M:%S",
    ):
        try:
            d = datetime.strptime(ts, fmt)
            if fmt in (
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S",
            ):
                # Z-suffixed and naive ISO pipeline state stamps are UTC.
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


def _sonarr_ep_lookup(cfg, ep_id):
    """GET Sonarr /api/v3/episode/{id} -> (status_code, episode dict|None).

    The single HTTP path shared by _ep_detail (library details) and
    validate_retry_target, so action validation does not add a new request
    shape. 404 is returned as-is so callers can distinguish ghosts from
    generic failures; unreachable Sonarr yields status 0."""
    url_base = (cfg or {}).get("SONARR_URL", "").rstrip("/")
    if not url_base:
        return 0, None
    code, data = _http(
        "GET",
        url_base + f"/episode/{ep_id}?format=json",
        headers={"X-Api-Key": (cfg or {}).get("SONARR_API_KEY", "")},
        timeout=10,
    )
    return code, data if isinstance(data, dict) else None


def validate_retry_target(cfg, ep_id, lookup=None):
    """Validate that a retry/delete action can actually be processed.

    Returns (True, None) when the episode exists in Sonarr, is monitored and
    has a video file; otherwise (False, <reason>). `lookup` overrides the
    Sonarr fetch (test seam); default is _sonarr_ep_lookup."""
    if lookup is None:
        lookup = _sonarr_ep_lookup
    code, data = lookup(cfg, ep_id)
    if code == 404:
        return False, "episode deleted from Sonarr; state will be pruned"
    if code != 200 or not data:
        return False, f"cannot verify episode in Sonarr (HTTP {code or 'error'})"
    if not data.get("hasFile"):
        return False, "episode has no video file; cannot process"
    ef = data.get("episodeFile") or {}
    if not ef.get("path"):
        return False, "episode has no video file; cannot process"
    if data.get("monitored") is False:
        return False, "episode unmonitored in Sonarr; monitor it first"
    return True, None


def _bazarr_movies_fetch(cfg):
    """Bazarr movies list (plain fetch, no cache): {"total", "data": [...]}.
    Same call shape as the ControlAPIv2._bazarr_movies cached method."""
    url_base = (cfg or {}).get("BAZARR_URL", "").rstrip("/")
    if not url_base:
        return {"total": 0, "data": []}
    code, data = _http(
        "GET",
        url_base + "/movies?start=0&length=500",
        headers={"X-API-KEY": (cfg or {}).get("BAZARR_API_KEY", "")},
        timeout=10,
    )
    if code != 200 or not isinstance(data, dict):
        return {"total": 0, "data": []}
    return data


def validate_movie_target(cfg, radarr_id, lookup=None):
    """Validate that a movie retry/delete action can actually be processed.

    Returns (True, None) when the movie exists in Bazarr/Radarr, is monitored
    and has a video file path; otherwise (False, <reason>). `lookup` overrides
    the Bazarr movies fetch (test seam); default is _bazarr_movies_fetch."""
    if lookup is None:
        def _lookup(cfg, rid):
            return next(
                (
                    m
                    for m in (_bazarr_movies_fetch(cfg).get("data") or [])
                    if m.get("radarrId") == rid
                ),
                None,
            )

        lookup = _lookup
    m = lookup(cfg, radarr_id)
    if not m:
        return False, "movie not found in Bazarr/Radarr"
    if m.get("monitored") is False:
        return False, "movie unmonitored in Radarr; monitor it first"
    if not m.get("path"):
        return False, "movie has no video file; cannot process"
    return True, None


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
        "REGISTRY_FILE": REGISTRY_FILE,
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
            self._h_webhook_test,
        )
        self.endpoints = {
            "/api2/status": self._h_status,
            "/api2/provenance": self._h_provenance,
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
            "/api2/webhook/test": self._h_webhook_test,
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
                    keym = re.fullmatch(r"(?:e|m):(\d+)", mid)
                    if keym:
                        return handler, {
                            "id": int(keym.group(1)),
                            "kind": "movie" if mid.startswith("m:") else "series",
                        }
                    if mid.isdigit():
                        return handler, {"id": int(mid), "kind": "series"}
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
            kind = e.get("kind") or "series"
            key = (kind, ep)
            cur = latest.get(key)
            cur_epoch = _parse_ts(cur.get("ts")) if cur else None
            new_epoch = _parse_ts(e.get("ts"))
            if cur is None or (new_epoch or -1) >= (cur_epoch or -1):
                latest[key] = e
            lang = e.get("language")
            if lang is not None:
                key = (kind, ep, lang)
                lcur = latest_by_lang.get(key)
                lcur_epoch = _parse_ts(lcur.get("ts")) if lcur else None
                if lcur is None or (new_epoch or -1) >= (lcur_epoch or -1):
                    latest_by_lang[key] = e
        return entries, latest, latest_by_lang

    def _registry(self):
        """subtitle_registry.jsonl rows, cached with a TTL like the state
        reader. The ledger is append-only and a row describes the CURRENT
        on-disk {stem}.{lang}.srt sidecar, so the last row per key wins;
        legacy rows without a stem fall back to (episode_id, lang). Missing
        or unreadable file yields [] and never raises."""

        def _fn():
            rows = []
            try:
                with open(self.opts["REGISTRY_FILE"], encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(e, dict):
                            rows.append(e)
            except OSError:
                pass
            by_key = {}
            for r in rows:
                lang = r.get("lang")
                if not lang:
                    continue
                stem = r.get("stem")
                if stem:
                    by_key[(stem, lang)] = r
                    continue
                eid = r.get("episode_id")
                if isinstance(eid, int):
                    by_key[("ep", eid, lang)] = r
            return list(by_key.values())

        return self._cached("registry", 10, _fn)

    @staticmethod
    def _prov_breakdown(rows):
        """Pure breakdown of registry rows: totals by source_kind (embedded /
        external / unknown_kind) plus per-language counters
        {embedded, external, unknown, total} where unknown counts rows with
        no source_kind. by_lang totals always sum to rows."""
        totals = {"rows": len(rows), "embedded": 0, "external": 0, "unknown_kind": 0}
        by_lang = {}
        for r in rows:
            kind = r.get("source_kind")
            if kind == "embedded":
                totals["embedded"] += 1
            elif kind == "external":
                totals["external"] += 1
            else:
                totals["unknown_kind"] += 1
            lang = LANG_NORM.get(r.get("lang") or "unknown", r.get("lang") or "unknown")
            entry = by_lang.setdefault(
                lang, {"embedded": 0, "external": 0, "unknown": 0, "total": 0}
            )
            if kind == "embedded":
                entry["embedded"] += 1
            elif kind == "external":
                entry["external"] += 1
            else:
                entry["unknown"] += 1
            entry["total"] += 1
        return totals, by_lang

    def _registry_summary(self, rows=None):
        """Counts over registry rows by source_kind (embedded / external /
        unknown) plus a per-language breakdown. `rows` may be passed in to
        reuse a single read across endpoints."""
        if rows is None:
            rows = self._registry()
        return self._prov_breakdown(rows)

    @staticmethod
    def _clean_stem(s):
        """Normalize a media stem for forgiving registry matching: drop
        "[AI-generated by ASRSub]" style tags, stray .srt/.ass suffixes and
        collapsed whitespace."""
        if not isinstance(s, str):
            return ""
        s = re.sub(r"\[AI-generated by ASRSub\]", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\.(srt|ass)$", "", s, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", s).strip(" .")

    def _registry_stems(self, path):
        """Candidate registry stems for a media path: the path itself with
        the extension stripped, the /data/... container path mapped onto
        NAS_MEDIA_ROOT (the tdarr webhook records NAS paths, Sonarr reports
        container paths), plus cleaned variants and a basename fallback so a
        registry row whose stem is only cosmetically different from the
        episode's media file still matches."""
        if not isinstance(path, str) or not path:
            return []
        stems = [os.path.splitext(path)[0]]
        root = self.opts.get("NAS_MEDIA_ROOT", "/mnt/nas/share/media").rstrip("/")
        if path.startswith("/data/"):
            stems.append(os.path.splitext(root + path[len("/data"):])[0])
        out = []
        for s in stems:
            if s not in out:
                out.append(s)
            c = self._clean_stem(s)
            if c and c not in out:
                out.append(c)
            b = self._clean_stem(os.path.basename(s))
            if b and b not in out:
                out.append(b)
        return out

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
                "current": None,
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
            "current": data.get("current"),
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

    def _bazarr_movies(self):
        def _fn():
            return _bazarr_movies_fetch(self._env())

        return self._cached("bazarr.movies", 30, _fn)

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
                raw_ts = h.get("parsed_timestamp")
                epoch = _parse_ts(raw_ts)
                ts_iso = None
                if epoch is not None:
                    ts_iso = datetime.fromtimestamp(
                        epoch, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
                out.append(
                    {
                        "ts": ts_iso or raw_ts,
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
        code, data = _sonarr_ep_lookup(self._env(), ep_id)
        if code == 200 and data:
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

    def _resolve_label(self, ep_id, wanted_data, movies_data=None):
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
            for mv in (movies_data or []):
                if mv.get("radarrId") == ep_id:
                    mtitle = mv.get("title") or f"movie {ep_id}"
                    return mtitle, "MOVIE", mtitle
            return None, f"ep {ep_id}", title
        return series, episode, title

    # ---------- endpoints ----------

    def _h_health(self, body, id=None):
        return 200, {"ok": True}

    def _h_provenance(self, body, id=None):
        rows = self._registry()
        totals, by_lang = self._registry_summary(rows)
        recent = sorted(
            rows, key=lambda r: (_parse_ts(r.get("ts")) or 0), reverse=True
        )[:20]
        recent_out = []
        for r in recent:
            stem = r.get("stem") or ""
            recent_out.append(
                {
                    "basename": os.path.basename(stem) or stem,
                    "lang": LANG_NORM.get(r.get("lang"), r.get("lang")),
                    "source_kind": r.get("source_kind"),
                    "source": r.get("source"),
                    "ts": r.get("ts"),
                    "audio_short": (r.get("audio_id") or "")[:8],
                }
            )
        return 200, {
            "totals": totals,
            "by_lang": by_lang,
            "recent": recent_out,
            "registry_path": self.opts["REGISTRY_FILE"],
            "updated_at": _now_iso(),
        }

    def _h_config(self, body, id=None):
        cfg = dict(CONFIG_DEFAULTS)
        cfg.update(self._env())
        return 200, self._mask(cfg)

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

    def _movies_remaining_local(self, movies=None):
        """Best-effort movies_remaining when the daemon did not report one
        (unreachable or pre-update daemon): monitored movies with a media
        file on disk that still lack AI-owned target subs. Mirrors the
        orchestrator sweep's per-movie gating: registry rows with source
        asr/jpn/eng own the sidecar, and an {stem}.i(n)d(.hi).srt target
        sidecar on disk blocks the id language."""
        if movies is None:
            movies = self._bazarr_movies()
        try:
            cfg = self._env()
            tl = cfg.get("TARGET_LANGS") or "id,en"
            if isinstance(tl, str) and tl.strip().startswith("["):
                try:
                    import ast

                    tl = ast.literal_eval(tl)
                except Exception:
                    tl = "id,en"
            if isinstance(tl, list):
                tl = ",".join(x for x in tl if isinstance(x, str))
            target_langs = {x.strip() for x in str(tl).split(",") if x.strip()}
            root = self.opts.get("NAS_MEDIA_ROOT", "/mnt/nas/share/media").rstrip("/")
            reg_rows = self._registry()
            reg_by_stem = {}
            for r in reg_rows:
                stem, lang = r.get("stem"), r.get("lang")
                if stem and lang:
                    reg_by_stem.setdefault(stem, {})[lang] = r
            remaining = 0
            for m in (movies.get("data") or []):
                if not isinstance(m.get("radarrId"), int):
                    continue
                if not m.get("monitored", True):
                    continue
                path = m.get("path") or ""
                if not path:
                    continue
                mapped = (
                    root + path[len("/data"):]
                    if isinstance(path, str) and path.startswith("/data/")
                    else path
                )
                if not os.path.isfile(mapped):
                    continue
                stems = [os.path.splitext(mapped)[0]]
                for lang in target_langs:
                    rec = None
                    for stem in stems:
                        rec = reg_by_stem.get(stem, {}).get(lang)
                        if rec:
                            break
                    if rec and rec.get("source") in ("asr", "jpn", "eng"):
                        continue
                    if lang == "id" and self._id_sidecar_exists(mapped):
                        continue
                    remaining += 1
                    break
            return remaining
        except Exception:
            return 0

    @staticmethod
    def _id_sidecar_exists(media_path):
        """Target-language (id) sidecar glob next to the media file; matches
        {stem}.id.srt / {stem}.id.hi.srt / {stem}.ind.srt / {stem}.ind.hi.srt."""
        d = os.path.dirname(media_path)
        basename_stem = os.path.splitext(os.path.basename(media_path))[0]
        rx = re.compile(
            r"^" + re.escape(basename_stem) + r"\.i(n)?d(\.hi)?\.srt$",
            re.IGNORECASE,
        )
        try:
            for name in os.listdir(d):
                if rx.match(name):
                    return True
        except OSError:
            pass
        return False

    def _h_status(self, body, id=None):
        entries, latest, _lbl = self._state()
        refine = self._refine_latest()
        daemon = self._daemon_status()
        wanted = self._bazarr_wanted()
        movies = self._bazarr_movies()
        gpu = self._gpu()
        llama = self._llama()
        episodes = {}
        for (kind, ep_id), st in latest.items():
            rf = refine.get(ep_id)
            episodes[f"{kind}:{ep_id}"] = {
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
        movie_total = sum(
            1
            for m in (movies.get("data") or [])
            if m.get("monitored", True) and (m.get("path") or "").strip()
        )
        movies_remaining = daemon.get("movies_remaining")
        if not isinstance(movies_remaining, int):
            movies_remaining = self._movies_remaining_local(movies)
        series_done = sum(
            1
            for k, st in episodes.items()
            if k.startswith("series:") and st.get("status") == "done"
        )
        series_remaining = wanted.get("total", 0)
        try:
            env = self._env()
        except Exception:
            env = {}
        asr_backend = env.get("ASR_BACKEND") or "whisper"
        models = {
            "asr_backend": asr_backend,
            "asr_model": env.get("SV_MODEL_ID") or "FunAudioLLM/SenseVoiceSmall",
            "vad_model": "fsmn-vad" if asr_backend == "sensevoice" else "silero",
            "emo_enabled": str(env.get("EMO_ENABLED") or "0").lower()
            in ("1", "true", "yes"),
            "emo_model": env.get("EMO_MODEL") or "emotion2vec/emotion2vec_plus_large",
        }
        _pa = self._read_pending_actions()
        pending_actions = {"total": len(_pa), "retry": 0, "delete": 0, "skip": 0}
        for rec in _pa:
            t = rec.get("type") or rec.get("action") or ""
            if t in pending_actions:
                pending_actions[t] += 1
        return 200, {
            "updated_at": _now_iso(),
            "daemon": daemon,
            "state_counts": counts,
            "queue": {"wanted": wanted.get("total", 0), "movies": movies_remaining},
            "movies": {"total": movie_total, "remaining": movies_remaining},
            "series": {
                "total": series_done + series_remaining,
                "done": series_done,
                "remaining": series_remaining,
            },
            "models": models,
            "gpu": gpu,
            "llama": llama,
            "registry": self._registry_block(),
            "episodes": episodes,
            "pending_actions": pending_actions,
        }

    def _registry_block(self):
        """Registry summary block for /api2/status ({rows, embedded,
        external, by_lang}); reuses the same reader as /api2/provenance."""
        totals, by_lang = self._registry_summary()
        return {
            "rows": totals["rows"],
            "embedded": totals["embedded"],
            "external": totals["external"],
            "unknown_kind": totals["unknown_kind"],
            "by_lang": by_lang,
        }

    def _h_activity(self, body, id=None):
        entries, _latest, _lbl = self._state()
        wanted = self._bazarr_wanted()
        movies = self._bazarr_movies()
        items = []
        for e in entries[-40:]:
            ep_id = e.get("sonarrEpisodeId")
            series, episode, title = self._resolve_label(
                ep_id, wanted.get("data", []) or [], movies.get("data") or []
            )
            items.append(
                {
                    "ts": e.get("ts"),
                    "kind": "movie" if e.get("kind") == "movie" else "pipeline",
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
            st = latest.get(("series", ep_id))
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
            for lang, lst in (
                (l, r)
                for (k, e, l), r in latest_by_lang.items()
                if k == "series" and e == ep_id
            ):
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

    def _lang_entries(self, langs_by_id, eid, stem_cands, reg_by_stem):
        """Per-language status entries for one library item, with provenance
        (source/source_kind) labels carried from the state records when
        present and enriched from registry rows matched by stem."""
        lang_entries = []
        for lang, st in sorted(langs_by_id.get(eid, {}).items()):
            entry = {
                "language": lang,
                "status": st["status"],
                "ts": st["ts"],
                "elapsed_s": st["elapsed_s"],
            }
            src = st.get("source")
            prov = st.get("source_kind")
            rec = None
            for stem in stem_cands:
                rec = reg_by_stem.get(stem, {}).get(lang)
                if rec:
                    break
            if rec:
                if not prov:
                    prov = rec.get("source_kind")
                if src is None:
                    src = rec.get("source")
            if prov:
                entry["prov"] = prov
            if src:
                entry["source"] = src
            lang_entries.append(entry)
        return lang_entries

    def _registry_lang_entries(self, langs_by_id, eid, stem_cands, reg_by_stem):
        """Merge languages that only exist in the subtitle registry (a
        sidecar on disk with no state row) into the per-episode lang map, so
        rows carry the provenance tags the lang chips were built for. Added
        entries are marked done with their registry source/source_kind."""
        entry = langs_by_id.get(eid)
        if entry is None:
            entry = {}
            langs_by_id[eid] = entry
        for stem in stem_cands:
            for lang, rec in (reg_by_stem.get(stem) or {}).items():
                lang = LANG_NORM.get(lang, lang)
                if lang in entry:
                    continue
                entry[lang] = {
                    "status": "done",
                    "ts": rec.get("ts"),
                    "elapsed_s": None,
                    "source": rec.get("source"),
                    "source_kind": rec.get("source_kind"),
                }
        return entry

    def _sonarr_all_episodes(self):
        """Enumerate every Sonarr episode across all series (scope=all
        library): {episode_id: {"series_id", "series_title", "season_number",
        "episode_number", "title", "path"}}. Returns None on any failure so
        the caller falls back to the active-only set."""
        cfg = self._env()
        url_base = cfg.get("SONARR_URL", "").rstrip("/")
        if not url_base:
            return None
        headers = {"X-Api-Key": cfg.get("SONARR_API_KEY", "")}
        try:
            code, data = _http("GET", url_base + "/series", headers=headers, timeout=15)
            if code != 200 or not isinstance(data, list):
                return None
            series_ids = [
                s.get("id")
                for s in data
                if isinstance(s, dict) and isinstance(s.get("id"), int)
            ]
            series_map = self._sonarr_series()

            def _fetch(sid):
                c, d = _http(
                    "GET", url_base + f"/episode?seriesId={sid}",
                    headers=headers, timeout=15,
                )
                if c != 200 or not isinstance(d, list):
                    return []
                return [
                    (e.get("id"), e)
                    for e in d
                    if isinstance(e, dict) and isinstance(e.get("id"), int)
                ]

            out = {}
            with ThreadPoolExecutor(max_workers=8) as ex:
                for pairs in ex.map(_fetch, series_ids):
                    for eid, e in pairs:
                        ef = e.get("episodeFile") or {}
                        out[eid] = {
                            "series_id": e.get("seriesId"),
                            "series_title": e.get("seriesTitle")
                            or series_map.get(e.get("seriesId")),
                            "season_number": e.get("seasonNumber"),
                            "episode_number": e.get("episodeNumber"),
                            "title": e.get("title"),
                            "path": ef.get("path"),
                        }
            return out
        except Exception:
            return None

    def _h_library(self, body, id=None):
        """Merged library: one item per episode across Bazarr wanted + state
        history + exclusions (series) plus one item per monitored Bazarr
        movie (kind "movie"), with per-language status. Read-only.

        scope=all (query param): additionally enumerate EVERY Sonarr episode
        (full library) so done/archived episodes stay visible; on enumeration
        failure it falls back to the active-only set. scope=inactive: return
        ONLY idle items (not wanted, not excluded, no languages) — series
        episodes and movies that have never been touched."""
        scope = "active"
        if isinstance(body, dict):
            scope = str(body.get("scope") or "active")
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
            if isinstance(r.get("episode_id"), int) and r.get("kind") != "movie"
        }
        excluded_movies = {
            -r.get("episode_id")
            for r in exclusions
            if r.get("kind") == "movie"
            and isinstance(r.get("episode_id"), int)
        }
        ep_ids = {
            eid
            for eid in (
                set(wanted_by_id)
                | {eid for (kind, eid) in latest if kind == "series"}
                | excluded_ids
            )
            if isinstance(eid, int)
        }
        all_ep = None
        if scope in ("all", "inactive"):
            all_ep = self._sonarr_all_episodes()
            if all_ep:
                ep_ids = ep_ids | set(all_ep)
            else:
                print(
                    "api2: library scope=all enumeration failed; using active set",
                    file=sys.stderr,
                    flush=True,
                )
        reg_rows = self._registry()
        details = {}
        if all_ep:
            # scope=all: seed details from the Sonarr enumeration so ~475
            # episodes don't each trigger a per-episode Sonarr call.
            series_map = self._sonarr_series()
            for eid, em in all_ep.items():
                details[eid] = {
                    "series": em.get("series_title")
                    or series_map.get(em.get("series_id"))
                    or "?",
                    "episode": self._episode_label(
                        em.get("season_number"), em.get("episode_number")
                    ),
                    "title": em.get("title"),
                    "series_id": em.get("series_id"),
                    "season_number": em.get("season_number"),
                    "episode_number": em.get("episode_number"),
                    "path": em.get("path"),
                    "quality": None,
                    "media": None,
                }
        detail_ids = [eid for eid in ep_ids if eid not in wanted_by_id and eid not in details]
        if reg_rows:
            # registry has rows: also resolve media paths for wanted items so
            # prov labels can match by stem (ep_detail is cached 300s)
            detail_ids = [eid for eid in ep_ids if eid not in details]
        if detail_ids:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for eid, det in zip(detail_ids, ex.map(self._ep_detail, detail_ids)):
                    details[eid] = det
        reg_by_stem = {}
        for r in reg_rows:
            stem, lang = r.get("stem"), r.get("lang")
            if stem and lang:
                lang = LANG_NORM.get(lang, lang)
                keys = [
                    stem,
                    self._clean_stem(stem),
                    self._clean_stem(os.path.basename(stem)),
                ]
                for k in keys:
                    if k:
                        reg_by_stem.setdefault(k, {})[lang] = r
        langs = {}
        for (kind, eid, lang), rec in latest_by_lang.items():
            if kind != "series":
                continue  # movie rows feed the movie items below, never series
            langs.setdefault(eid, {})[lang] = {
                "status": rec.get("status"),
                "ts": rec.get("ts"),
                "elapsed_s": rec.get("elapsed_s"),
                "source": rec.get("source"),
                "source_kind": rec.get("source_kind"),
            }
        for eid, w in wanted_by_id.items():
            entry = langs.setdefault(eid, {})
            for m in (w.get("missing_subtitles") or []):
                if not isinstance(m, dict):
                    continue
                code = m.get("code2")
                if code and code not in entry:
                    entry[code] = {"status": "wanted", "ts": None, "elapsed_s": None}
        for eid in ep_ids:
            self._registry_lang_entries(
                langs, eid, self._registry_stems((details.get(eid) or {}).get("path")), reg_by_stem
            )
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
            det = details.get(eid) or {}
            stem_cands = self._registry_stems(det.get("path"))
            items.append(
                {
                    "item_key": f"e:{eid}",
                    "kind": "series",
                    "sonarr_episode_id": eid,
                    "series": series,
                    "episode": episode,
                    "title": title,
                    "season": season,
                    "episode_number": epnum,
                    "wanted": eid in wanted_by_id,
                    "excluded": eid in excluded_ids,
                    "languages": self._lang_entries(langs, eid, stem_cands, reg_by_stem),
                }
            )
        movies_json = self._bazarr_movies()
        movie_langs = {}
        for (kind, eid, lang), rec in latest_by_lang.items():
            if kind == "movie" and isinstance(eid, int):
                movie_langs.setdefault(eid, {})[lang] = {
                    "status": rec.get("status"),
                    "ts": rec.get("ts"),
                    "elapsed_s": rec.get("elapsed_s"),
                    "source": rec.get("source"),
                    "source_kind": rec.get("source_kind"),
                }
        for m in (movies_json.get("data") or []):
            rid = m.get("radarrId")
            if not isinstance(rid, int):
                continue
            if not m.get("monitored", True):
                continue
            path = m.get("path") or ""
            if not path:
                continue
            title = m.get("title") or "?"
            stem_cands = self._registry_stems(path)
            self._registry_lang_entries(movie_langs, rid, stem_cands, reg_by_stem)
            movie_wanted = bool(m.get("missing_subtitles"))
            if (
                scope == "active"
                and not movie_wanted
                and not movie_langs.get(rid)
                and rid not in excluded_movies
            ):
                continue  # idle movie (no langs, not wanted, not excluded): all scope only
            items.append(
                {
                    "item_key": f"m:{rid}",
                    "kind": "movie",
                    "sonarr_episode_id": rid,
                    "series": title,
                    "episode": "MOVIE",
                    "title": title,
                    "season": None,
                    "episode_number": None,
                    "path": path,
                    "wanted": movie_wanted,
                    "excluded": rid in excluded_movies,
                    "languages": self._lang_entries(movie_langs, rid, stem_cands, reg_by_stem),
                }
            )
        if scope == "inactive":
            items = [
                it
                for it in items
                if not it.get("wanted")
                and not it.get("excluded")
                and not (it.get("languages") or [])
            ]
        pending_map = {}
        for rec in self._read_pending_actions():
            t = rec.get("type") or rec.get("action") or ""
            if t not in ("retry", "delete", "skip"):
                continue
            key = ("m:" if rec.get("kind") == "movie" else "e:") + str(
                rec.get("episode_id")
            )
            # prefer retry/delete over skip if both somehow queued for the item
            if key not in pending_map or (pending_map[key] == "skip" and t != "skip"):
                pending_map[key] = t
        for it in items:
            it["pending_action"] = pending_map.get(it.get("item_key"))
        items.sort(
            key=lambda it: (
                1 if it.get("kind") == "movie" else 0,
                it["series"] or "",
                it["season"] if isinstance(it["season"], int) else -1,
                it["episode_number"] if isinstance(it["episode_number"], int) else -1,
            )
        )
        movie_count = sum(1 for it in items if it.get("kind") == "movie")
        out = {"items": items[:1000], "total": len(items), "movies": movie_count}
        if len(items) > 1000:
            out["truncated"] = True
        return 200, out

    def _append_action(self, kind, ep_id, body, movie=False):
        if movie and kind == "skip":
            return 409, {"ok": False, "error": "action not available for movies"}
        if kind in ("retry", "delete"):
            if movie:
                ok, err = validate_movie_target(self._env(), ep_id)
            else:
                ok, err = validate_retry_target(self._env(), ep_id)
            if not ok:
                return 409, {"ok": False, "error": err}
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
        if movie:
            rec["kind"] = "movie"
        path = self.opts["ACTIONS_FILE"]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise ApiError(500, f"failed to write actions.jsonl: {exc}")
        return 200, {"ok": True, "record": rec}

    def _h_retry(self, body, id=None, kind="series"):
        return self._append_action("retry", id, body, movie=(kind == "movie"))

    def _h_skip(self, body, id=None, kind="series"):
        return self._append_action("skip", id, body, movie=(kind == "movie"))

    def _h_delete(self, body, id=None, kind="series"):
        return self._append_action("delete", id, body, movie=(kind == "movie"))

    def _lock_exclusions(self):
        """Exclusive flock on the exclusions file, held across the read-
        modify-write. Cross-process (unlike self._excl_lock, which only
        guards threads of this process). Returns the fd to unlock/close."""
        path = self.opts["EXCLUSIONS_FILE"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            os.close(fd)
            raise
        return fd

    def _unlock_exclusions(self, fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

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

    def _read_pending_actions(self):
        """Pending (unconsumed) actions.jsonl records. Read-only, no flock
        needed for display purposes; malformed lines skipped. Never raises."""
        recs = []
        try:
            with open(self.opts["ACTIONS_FILE"], encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(e, dict) and isinstance(e.get("episode_id"), int):
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
        out = []
        for r in recs:
            rec = dict(r)
            if rec.get("kind") == "movie" and isinstance(rec.get("episode_id"), int):
                rec["key"] = "m:" + str(-rec["episode_id"])
            elif isinstance(rec.get("episode_id"), int):
                rec["key"] = "e:" + str(rec["episode_id"])
            out.append(rec)
        return 200, {
            "total": len(out),
            "exclusions": out,
            "updated_at": _now_iso(),
        }

    def _h_exclude(self, body, id=None, kind="series"):
        reason = ""
        if isinstance(body, dict) and isinstance(body.get("reason"), str):
            reason = body["reason"]
        if kind == "movie":
            series_id = None
            store_id = -id  # negative ids never collide with Sonarr episode ids
        else:
            wanted = self._bazarr_wanted()
            series_id = self._ep_series_id(id, wanted.get("data", []) or [])
            if series_id is None:
                series_id = self._ep_detail(id).get("series_id")
            store_id = id
        rec = {
            "episode_id": store_id,
            "series_id": series_id,
            "reason": reason,
            "ts": _now_iso(),
        }
        if kind == "movie":
            rec["kind"] = "movie"
        with self._excl_lock:
            lock_fd = self._lock_exclusions()
            try:
                for r in self._read_exclusions():
                    if r.get("episode_id") == store_id and (
                        (r.get("kind") == "movie") == (kind == "movie")
                    ):
                        return 200, {"ok": True, "already_excluded": True, "record": r}
                try:
                    recs = self._read_exclusions()
                    recs.append(rec)
                    self._write_exclusions(recs)
                except OSError as exc:
                    raise ApiError(500, f"failed to write exclusions.jsonl: {exc}")
            finally:
                self._unlock_exclusions(lock_fd)
        return 200, {"ok": True, "record": rec}

    def _h_unexclude(self, body, id=None, kind="series"):
        store_id = -id if kind == "movie" else id
        with self._excl_lock:
            lock_fd = self._lock_exclusions()
            try:
                recs = self._read_exclusions()
                before = len(recs)
                recs = [
                    r
                    for r in recs
                    if not (
                        r.get("episode_id") == store_id
                        and ((r.get("kind") == "movie") == (kind == "movie"))
                    )
                ]
                try:
                    self._write_exclusions(recs)
                except OSError as exc:
                    raise ApiError(500, f"failed to write exclusions.jsonl: {exc}")
            finally:
                self._unlock_exclusions(lock_fd)
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

    def _h_webhook_test(self, body, id=None):
        """Send a test event to configured webhook URLs (or the single URL
        from the body) and report per-URL HTTP status + latency. Uses the
        same HMAC scheme as the daemon's outbound webhooks. No retry."""
        try:
            import orchestrator as _orch

            cfg = _orch.load_config()
        except Exception:
            cfg = {}
        secret = cfg.get("WEBHOOK_SECRET") or cfg.get("HERMES_WEBHOOK_SECRET") or ""
        urls = []
        if isinstance(body, dict) and body.get("url"):
            urls = [str(body["url"])]
        else:
            urls = [u.strip() for u in str(cfg.get("WEBHOOK_URLS") or "").split(",") if u.strip()]
            if not urls:
                legacy = (cfg.get("HERMES_WEBHOOK_URL") or "").strip()
                if legacy:
                    urls = [legacy]
        if not urls:
            return 200, {"ok": True, "configured": False, "results": []}
        payload = {
            "event_type": "webhook_test",
            "test": True,
            "ts": _now_iso(),
            "note": "ASRSub webhook test",
        }
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        results = []
        for url in urls:
            t0 = time.time()
            try:
                ts = str(int(time.time()))
                headers = {"X-Webhook-Timestamp": ts, "Content-Type": "application/json"}
                if secret:
                    sig = hmac.new(
                        secret.encode(), ts.encode() + b"." + body_bytes, hashlib.sha256
                    ).hexdigest()
                    headers["X-Webhook-Signature-V2"] = sig
                req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        status = resp.status
                except urllib.error.HTTPError as exc:
                    status = exc.code
                results.append(
                    {
                        "url": url,
                        "status": status,
                        "latency_ms": int((time.time() - t0) * 1000),
                        "error": None,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "url": url,
                        "status": None,
                        "latency_ms": int((time.time() - t0) * 1000),
                        "error": str(exc),
                    }
                )
        return 200, {"ok": True, "configured": True, "results": results}


class _RequestHandler(BaseHTTPRequestHandler):
    def _handle(self, method):
        api = getattr(self.server, "api2", None)
        if api is None:
            self._send(500, {"error": "api2 not registered"})
            return
        raw_path = self.path.split("?", 1)
        path = raw_path[0]
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
        elif len(raw_path) > 1:
            # read-only GET query params (e.g. /api2/library?scope=all)
            params = urllib.parse.parse_qs(raw_path[1])
            body = {k: v[0] for k, v in params.items()}
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
