#!/usr/bin/env python3
"""Direct Jimaku REST client: bypasses Bazarr's provider search for Japanese
subtitles (verified live 2026-08-25 against https://jimaku.cc).

Facts baked in (do not re-litigate):
  - Auth is a RAW key in the Authorization header; the 'Bearer ' prefix
    returns 401. The key lives in pipeline.env as JIMAKU_API_KEY=<raw>.
  - Rate limit is 25 req/min per IP; 429 responses carry
    x-ratelimit-reset-after (surfaced on JimakuRateLimited.reset_after).
  - GET /api/entries/search?anilist_id=<id> returns a TOP-LEVEL JSON LIST of
    entries [{id, japanese_name, name, anilist_id, ...}] (not an object).
  - GET /api/entries/{entry_id}/files?episode=N returns a JSON list of
    [{url, name, size, last_modified}]; the episode param filters correctly.
  - File URLs (https://jimaku.cc/entry/<id>/download/<name>) download with
    the same auth header; files may be .srt OR .ass (the ladder's retime
    path already converts .ass -> srt via ffmpeg).

Design: stdlib + requests only. No retries inside the client — callers own
retry/backoff policy (the orchestrator's hunt/ladder flows already do). A
tiny pacing sleep between consecutive calls keeps one candidate attempt far
below the rate limit. Optional session/base_url injection makes every call
unit-testable without network. This module never logs: callers own logging.
"""

import json
import os
import re
import time
from datetime import datetime

import requests

DEFAULT_BASE_URL = os.environ.get("JIMAKU_BASE_URL", "https://jimaku.cc/api")
DEFAULT_TIMEOUT = float(os.environ.get("JIMAKU_TIMEOUT", "30"))
# Pacing between consecutive Jimaku calls within one client (a candidate
# attempt makes ~3 calls: search + files + download).
DEFAULT_CALL_SLEEP = float(os.environ.get("JIMAKU_CALL_SLEEP", "0.5"))

ANILIST_URL = "https://graphql.anilist.co"
ANILIST_TIMEOUT = float(os.environ.get("ANILIST_TIMEOUT", "30"))

# Same disk cache fetch_glossary.py maintains (keyed by tolerant-normalized
# series title, entries carry media_id); shared so one lookup serves both.
DEFAULT_ANILIST_CACHE = os.path.join(
    os.path.expanduser("~"), ".config", "asr-pipeline", "anilist_cache.json"
)

_ANILIST_QUERY = """
query($search: String) {
  Media(search: $search, type: ANIME) {
    id
    title { romaji english native }
    format
  }
}
"""

# Subtitle container extensions the ladder can actually consume (.ass/.ssa go
# through ffmpeg conversion in the retime path). Everything else (nfo/zip/...)
# is ignored by rank_files.
SUB_EXTS = (".srt", ".ass", ".ssa")

# CHS/CHT bilingual variants (Chinese + Japanese tracks): demoted whenever any
# JPN-only file exists so they are only chosen as a last resort. The penalty
# outweighs every positive bonus combined (max stack 13) so even a fully
# tag-stacked bilingual file loses to ANY plain JPN-only subtitle.
_BILINGUAL_TOKENS = {"chs", "cht", "big5"}
_BILINGUAL_PENALTY = -14

# Fansub groups seen on Jimaku archives; their .ass/.srt files are the
# second-choice class after release-tag/plain-S01E01 .srt files.
_FANSUB_TOKENS = {"kitaujisub", "kitauji", "lolihouse", "nanakoraws"}

_EP_TAG_RE = re.compile(r"S\d{1,2}E\d{1,2}", re.IGNORECASE)


class JimakuError(RuntimeError):
    """Any non-429 Jimaku API/download failure (HTTP status, bad json,
    unexpected shape, network error). Callers log and fall through."""


class JimakuRateLimited(JimakuError):
    """HTTP 429 from Jimaku (25 req/min per IP). Carries reset_after from the
    x-ratelimit-reset-after response header when present."""

    def __init__(self, message, reset_after=None):
        super().__init__(message)
        self.reset_after = reset_after


def _norm_title(s):
    return " ".join((s or "").strip().lower().split())


def _cache_key(title):
    """Anilist cache key: byte-for-byte the same normalization as
    fetch_glossary.tolerant (lowercase, whitespace-collapsed, then x/x-dash
    swaps: U+00D7->x, U+30FB->space, ':'->space, apostrophes stripped) so
    entries written there are found here and vice versa."""
    s = _norm_title(title)
    return (
        s.replace("\u00d7", "x")
        .replace("\u30fb", " ")
        .replace(":", " ")
        .replace("'", "")
    )


def _cache_path(cfg=None):
    path = None
    if isinstance(cfg, dict):
        path = cfg.get("ANILIST_CACHE")
    return path or os.environ.get("ANILIST_CACHE") or DEFAULT_ANILIST_CACHE


def _load_cache(path):
    """Corruption-safe cache read: missing/unparseable starts fresh."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_cache(path, cache):
    """Atomic write (tmp + os.replace), mirroring fetch_glossary.save_cache;
    failures are swallowed (cache is an optimization)."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


class JimakuClient:
    """Small requests-based Jimaku client (raw-key auth, no retries).

    session/base_url/timeout/call_sleep are injectable for unit tests;
    api_key defaults to the JIMAKU_API_KEY env var when not given."""

    def __init__(self, api_key=None, session=None, base_url=None, timeout=None,
                 call_sleep=None):
        self.api_key = str(
            api_key if api_key is not None else os.environ.get("JIMAKU_API_KEY") or ""
        ).strip()
        self.session = session if session is not None else requests.Session()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
        self.call_sleep = (
            call_sleep if call_sleep is not None else DEFAULT_CALL_SLEEP
        )
        self._last_call = 0.0

    def _pace(self):
        """Tiny sleep so consecutive calls inside one attempt never burst."""
        if self.call_sleep <= 0:
            return
        wait = self._last_call + self.call_sleep - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _auth_headers(self):
        # RAW key: Jimaku 401s when the 'Bearer ' prefix is present.
        return {"Authorization": self.api_key, "Accept": "application/json"}

    def _get_json(self, path, params=None):
        """GET + JSON decode with dedicated 429 handling. Raises
        JimakuRateLimited / JimakuError; no retries."""
        self._pace()
        try:
            resp = self.session.get(
                self.base_url + path,
                params=params,
                headers=self._auth_headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise JimakuError(f"request failed: {exc}") from exc
        _raise_for_status(resp)
        try:
            return resp.json()
        except ValueError as exc:
            raise JimakuError(f"bad json from {path}: {exc}") from exc

    def search_by_anilist(self, anilist_id):
        """Entries matching an AniList id -> list[entry dict]. The endpoint
        returns a top-level JSON list (verified live)."""
        return _entries_list(
            self._get_json("/entries/search", {"anilist_id": int(anilist_id)})
        )

    def list_files(self, entry_id, episode=None):
        """Files for one entry -> list[file dict]; episode=N filters to that
        episode server-side (omit the param for the full listing)."""
        params = {"episode": int(episode)} if episode is not None else None
        return _entries_list(self._get_json(f"/entries/{int(entry_id)}/files", params))

    def download(self, url, dest_path):
        """Stream a subtitle file (entry download URL) to dest_path with the
        same raw auth header. Writes to '<dest>.part' and atomically renames
        so a killed download never leaves a half-written sidecar. Returns
        dest_path; raises JimakuRateLimited/JimakuError."""
        self._pace()
        tmp = dest_path + ".part"
        parent = os.path.dirname(dest_path)
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
            with self.session.get(
                url, headers={"Authorization": self.api_key},
                stream=True, timeout=self.timeout,
            ) as resp:
                _raise_for_status(resp)
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            fh.write(chunk)
            os.replace(tmp, dest_path)
        except requests.RequestException as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise JimakuError(f"download failed: {url}: {exc}") from exc
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return dest_path


def _raise_for_status(resp):
    """Shared status handling: 429 -> JimakuRateLimited (reset_after parsed),
    other non-2xx -> JimakuError with a short body snippet."""
    if resp.status_code == 429:
        reset_after = (
            resp.headers.get("x-ratelimit-reset-after")
            or resp.headers.get("Retry-After")
        )
        raise JimakuRateLimited(
            "HTTP 429 rate limited" + (f" reset_after={reset_after}" if reset_after else ""),
            reset_after=reset_after,
        )
    if not (200 <= resp.status_code < 300):
        snippet = ""
        try:
            snippet = (resp.text or "")[:200]
        except Exception:
            pass
        raise JimakuError(f"HTTP {resp.status_code}: {snippet}")


def _entries_list(data):
    """Parse an entries/files response: a TOP-LEVEL JSON LIST (verified);
    a {"entries": [...]} object wrapper is tolerated defensively. Non-dict
    items are dropped. Anything else raises JimakuError."""
    items = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("entries"), list):
        items = data["entries"]
    if items is None:
        raise JimakuError("unexpected response shape (expected a JSON list)")
    return [it for it in items if isinstance(it, dict)]


def pick_entry(entries, anilist_id=None):
    """Entry selection: prefer an entry whose anilist_id matches exactly
    (int or numeric string), else take the first entry. Returns None when no
    usable entry exists."""
    clean = [e for e in (entries or []) if isinstance(e, dict)]
    if not clean:
        return None
    if anilist_id is not None:
        try:
            want = int(anilist_id)
        except (TypeError, ValueError):
            want = None
        if want is not None:
            for e in clean:
                try:
                    if int(e.get("anilist_id")) == want:
                        return e
                except (TypeError, ValueError):
                    continue
    return clean[0]


def rank_files(files):
    """Rank Jimaku file candidates best-first (stable: ties keep listing
    order). Scoring tiers mirror what has been seen working live:
      - only subtitle containers are kept (.srt/.ass/.ssa);
      - .srt whose name carries the release tags seen working (VARYG /
        CR web-dl token names) or plain S01Exx naming wins;
      - fansub group files (KitaujiSub/LoliHouse/NanakoRaws) next;
      - plain .srt over plain .ass;
      - CHS/CHT bilingual variants sink below ANY JPN-only file (only used
        as a last resort)."""
    scored = []
    for f in files or []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "")
        ext = os.path.splitext(name)[1].lower()
        if ext not in SUB_EXTS:
            continue
        toks = {t.lower() for t in re.split(r"[^A-Za-z0-9]+", name) if t}
        score = 3 if ext == ".srt" else 1
        if "varyg" in toks or "cr" in toks:
            score += 4
        if _EP_TAG_RE.search(name):
            score += 4
        if toks & _FANSUB_TOKENS:
            score += 2
        if toks & _BILINGUAL_TOKENS:
            score += _BILINGUAL_PENALTY
        scored.append((score, f))
    scored.sort(key=lambda sf: sf[0], reverse=True)
    return [f for _, f in scored]


def resolve_anilist_id(cfg, series_title, log=None, session=None):
    """AniList media id for a series title, anilist_cache.json-first (the
    same cache fetch_glossary.py writes: keyed by tolerant-normalized title,
    entries carry media_id). On a miss queries graphql.anilist.co once (same
    Media(search:, type: ANIME) pattern as fetch_glossary.anilist_query) and
    merges media_id/title_romaji/fetched_at into the existing entry (any
    characters list is preserved) before writing the cache back atomically.
    Cache ids have no TTL here: AniList ids are stable, and re-resolving
    would only burn API budget. Never raises; returns None on failure
    (empty title, query failure, no match)."""
    title = str(series_title or "").strip()
    if not title:
        return None
    path = _cache_path(cfg)
    cache = _load_cache(path)
    key = _cache_key(title)
    entry = cache.get(key)
    if isinstance(entry, dict):
        try:
            mid = int(entry.get("media_id"))
        except (TypeError, ValueError):
            mid = 0
        if mid > 0:
            return mid
    media = _anilist_search(title, session=session)
    if not isinstance(media, dict):
        return None
    try:
        mid = int(media.get("id"))
    except (TypeError, ValueError):
        return None
    if mid <= 0:
        return None
    merged = entry if isinstance(entry, dict) else {}
    merged["fetched_at"] = datetime.now().isoformat()
    merged["media_id"] = mid
    merged["title_romaji"] = ((media.get("title") or {}).get("romaji") or "")
    cache[key] = merged
    _save_cache(path, cache)
    if log:
        log(f"ladder: jimaku direct: resolved '{title}' -> AniList {mid} (cached)")
    return mid


def _anilist_search(title, session=None):
    """Single-attempt AniList GraphQL search -> Media dict or None. Never
    raises (resolver contract: fall through silently on failure)."""
    poster = session if session is not None else requests
    # Same tokenizer quirk as fetch_glossary.anilist_search_str.
    query_title = title.replace("\u00d7", "x")
    try:
        r = poster.post(
            ANILIST_URL,
            json={"query": _ANILIST_QUERY, "variables": {"search": query_title}},
            headers={
                "User-Agent": "asr-pipeline-jimaku-direct/1.0",
                "Accept": "application/json",
            },
            timeout=ANILIST_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
    except Exception:
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None
    media = data.get("Media")
    return media if isinstance(media, dict) else None
