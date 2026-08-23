#!/usr/bin/env python3
"""AniList character-name glossary fetcher for the ASR pipeline.

Fetches per-series character names from AniList and merges kana-reading
keys into ~/.config/asr-pipeline/glossary.json so new shows/movies get
translation terminology without manual hardcoding.

Pipeline contract (pipeline/glossary.py): glossary.json maps
    {"Series Title": {"アーシア": "Asia", ...}}
keys are KANA readings (ASR/SenseVoice never outputs kanji), values are
English/romaji names. knowledge_block() renders official-name entries for the series.

Name mapping rules (per character, MAIN first):
  - ja_key:  native name if it is pure katakana/hiragana (foreign-origin
             names like リアス・グレモリー), else pykakasi katakana reading
             of the native (kanji-origin names like 兵藤一誠 -> ヒョウドウイッセイ).
             ・ separators are preserved in the reading.
  - short forms: foreign names -> first ・ segment (リアス); kanji-origin
             names -> last segment of the reading (given name, イッセイ).
  - en_value: first token of name.full (Rias) and the full name
             (Rias Gremory) as an additional pair.
  - capped at 15 new pairs per series, MAIN characters first.
Existing entries are never clobbered (only missing keys are added).

Usage:
  python3 fetch_glossary.py --series "High School DxD"
  python3 fetch_glossary.py --all
  python3 fetch_glossary.py --series "Title" --glossary /tmp/test-glossary.json

Cache:
  AniList responses are cached on disk in
  ~/.config/asr-pipeline/anilist_cache.json (env override ANILIST_CACHE),
  keyed by normalized title. A fresh entry (younger than the TTL, default
  90 days; env override ANILIST_CACHE_TTL_DAYS) short-circuits the API
  entirely; stale entries are re-fetched and refreshed. Failed lookups
  (no match / no characters) are never cached. Series whose glossary
  entry already holds the 15-ref cap are skipped without any API call.

Env overrides: SONARR_URL, SONARR_API_KEY, RADARR_URL, RADARR_API_KEY,
GLOSSARY_FILE, ANILIST_CACHE, ANILIST_CACHE_TTL_DAYS.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import deque
from datetime import datetime

import requests

try:
    import pykakasi
except ImportError:
    print("pykakasi is required: pip install pykakasi", file=sys.stderr)
    sys.exit(2)

ANILIST_URL = "https://graphql.anilist.co"
ANILIST_HEADERS = {"User-Agent": "asr-pipeline-glossary-fetcher/1.0", "Accept": "application/json"}

SONARR_URL = os.environ.get("SONARR_URL", "http://10.10.20.160:8989/api/v3")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "bafffc4b26ec4bfd9619a69343f06b91")
RADARR_URL = os.environ.get("RADARR_URL", "http://10.10.20.160:7878/api/v3")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "18122003a12645e2862a8de4945138bf")

GLOSSARY_FILE = os.environ.get(
    "GLOSSARY_FILE",
    os.path.join(os.path.expanduser("~"), ".config", "asr-pipeline", "glossary.json"),
)

CACHE_FILE = os.environ.get(
    "ANILIST_CACHE",
    os.path.join(os.path.expanduser("~"), ".config", "asr-pipeline", "anilist_cache.json"),
)
CACHE_TTL_DAYS = float(os.environ.get("ANILIST_CACHE_TTL_DAYS", "90"))

MAX_PAIRS_PER_SERIES = 15  # consumer cap in pipeline/glossary.py

_api_calls = 0  # AniList GraphQL calls made this run (verification aid)

_kks = pykakasi.kakasi()

# katakana/hiragana + katakana phonetic ext + ・ 、ー whitespace
_KANA_RE = re.compile(r"^[\u3040-\u30ff\u31f0-\u31ff\u3000\s・ー-]+$")
_READING_OK_RE = re.compile(r"^[\u3040-\u30ff\u31f0-\u31ff\s・ー]+$")

ROLE_PRIORITY = {"MAIN": 0, "SUPPORTING": 1, "BACKGROUND": 2}

# ---------------------------------------------------------------------------
# AniList client (rate-limited, retried)
# ---------------------------------------------------------------------------


class AniListRateLimiter:
    """Keeps us comfortably under AniList's 90 requests/minute."""

    def __init__(self, max_per_min=80):
        self.max = max_per_min
        self.calls = deque()

    def wait(self):
        now = time.time()
        while self.calls and now - self.calls[0] > 60:
            self.calls.popleft()
        if len(self.calls) >= self.max:
            sleep_until = self.calls[0] + 60
            time.sleep(max(0.1, sleep_until - time.time()))
            self.calls.popleft()
        self.calls.append(time.time())


_limiter = AniListRateLimiter()

SEARCH_QUERY = """
query($search: String, $id: Int) {
  Media(search: $search, id: $id, type: ANIME) {
    id
    title { romaji english native }
    format
    characters(page: 1, perPage: 25, sort: ROLE) {
      edges {
        role
        node { name { full native alternative } }
      }
    }
  }
}
"""

SEARCH_QUERY_FORMAT = """
query($search: String, $format: MediaFormat) {
  Media(search: $search, type: ANIME, format: $format) {
    id
    title { romaji english native }
    format
    characters(page: 1, perPage: 25, sort: ROLE) {
      edges {
        role
        node { name { full native alternative } }
      }
    }
  }
}
"""


def anilist_query(variables, query=SEARCH_QUERY, attempts=5):
    global _api_calls
    _api_calls += 1
    # AniList 404s when a variable is explicitly null; omit absent args.
    variables = {k: v for k, v in (variables or {}).items() if v is not None}
    for i in range(attempts):
        _limiter.wait()
        try:
            r = requests.post(
                ANILIST_URL,
                json={"query": query, "variables": variables},
                headers=ANILIST_HEADERS,
                timeout=30,
            )
        except requests.RequestException:
            if i == attempts - 1:
                raise
            time.sleep(2 ** i)
            continue
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 2 ** i))
            time.sleep(wait)
            continue
        if r.status_code == 404:
            return {"data": {"Media": None}}  # AniList 404 = no Media match
        if r.status_code >= 500:
            if i == attempts - 1:
                r.raise_for_status()
            time.sleep(2 ** i)
            continue
        r.raise_for_status()
        body = r.json()
        if "errors" in body:
            raise RuntimeError("AniList error: " + json.dumps(body["errors"]))
        return body
    raise RuntimeError("AniList request failed after retries")


# ---------------------------------------------------------------------------
# Name -> (kana key, short key, english values)
# ---------------------------------------------------------------------------


def is_kana(s):
    return bool(_KANA_RE.match(s or ""))


def to_katakana(native):
    """pykakasi reading of the native name, spaces stripped, ・ kept.
    々 (iteration mark) is expanded to the preceding character first;
    pykakasi leaves it unconverted otherwise."""
    native = expand_nooma(native)
    out = []
    for item in _kks.convert(native):
        k = item["kana"] or ""
        out.append(k)
    return re.sub(r"\s+", "", "".join(out))


def expand_nooma(s):
    """『々』 repeats the preceding character; expand it so pykakasi can
    convert (e.g. 花々子 -> 花花子)."""
    out = []
    prev = ""
    for ch in s:
        if ch == "\u3005":
            out.append(prev or "")
        else:
            out.append(ch)
            prev = ch
    return "".join(out)


def split_segments(reading):
    """Split a reading on ・ / whitespace, dropping empties."""
    return [s for s in re.split(r"[・\s]+", reading) if s]


def build_pairs(characters):
    """characters: [{role, full, native, alternative}...]; returns
    [(ja, en, aliases), ...] capped at MAX_PAIRS_PER_SERIES, MAIN first.

    aliases: AniList alternative spellings that are NOT already represented
    by the ja reading or en value themselves (case-folded check)."""
    ranked = sorted(
        characters,
        key=lambda c: (ROLE_PRIORITY.get(c.get("role"), 9)),
    )
    pairs = []
    seen = set()

    def _norm_alias(a):
        return (a or "").strip().lower()

    for c in ranked:
        native = (c.get("native") or "").strip()
        full = (c.get("full") or "").strip()
        if not native or not full:
            continue
        if is_kana(native):
            reading = native
        else:
            reading = to_katakana(native)
        if not reading or not _READING_OK_RE.match(reading):
            continue  # residual kanji/symbols would never match ASR kana output
        tokens = [t for t in full.split() if t]
        first = tokens[0] if tokens else full
        segments = split_segments(reading)
        if is_kana(native) and len(segments) > 1:
            short = segments[0]  # foreign names: first ・ segment
        else:
            short = segments[-1] if segments else reading  # kanji: given name
        # alias pool from AniList alternative[] minus forms we already carry
        covered = {
            _norm_alias(reading),
            _norm_alias(short),
            _norm_alias(first),
            _norm_alias(full),
            _norm_alias(native),
        }
        aliases_out = []
        for a in c.get("alternative") or []:
            na = (a or "").strip()
            if not na or _norm_alias(na) in covered:
                continue
            if any(_norm_alias(x) == _norm_alias(na) for x in aliases_out):
                continue
            aliases_out.append(na)
            covered.add(_norm_alias(na))
        cand = [
            (reading, first, aliases_out),
            (reading, full, []),
            (short, first, []),
        ]
        for ja, en, aliases in cand:
            if not ja or not en:
                continue
            dup = (ja, en) in seen
            seen.add((ja, en))
            if dup:
                continue
            pairs.append((ja, en, aliases))
            if len(pairs) >= MAX_PAIRS_PER_SERIES:
                return pairs
    return pairs


# ---------------------------------------------------------------------------
# Glossary merge (never clobbers existing keys)
# ---------------------------------------------------------------------------


def norm(s):
    return " ".join((s or "").strip().lower().split())


def tolerant(s):
    """Title normalization tolerant of ×/x, ・, colon/apostrophe drift."""
    s = norm(s)
    return s.replace("×", "x").replace("・", " ").replace(":", " ").replace("'", "")


def find_existing_key(glossary, title):
    norm_t = norm(title)
    for k in glossary:
        if norm(k) == norm_t:
            return k
    tol_t = tolerant(title)
    for k in glossary:
        if tolerant(k) == tol_t:
            return k
    return None


def load_glossary(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def merge_pairs(glossary, title, pairs):
    """Returns (key, added, existing_count, skipped_full) — v2 aware.

    Ensures glossary[key] is a v2 dict {"entries": [...]}, migrating a legacy
    flat-map series in place if needed. pairs items are (ja, en) or
    (ja, en, aliases). New entries are v2-shaped with kind="character".
    Aliases carried by a pair are BACKFILLED onto the matching existing
    entry (by ja or en) instead of being dropped — this counts toward
    `added` even when the series is at the row cap, so alias enrichment
    works on full series without touching production name rows.
    """
    key = find_existing_key(glossary, title) or title
    raw = glossary.get(key)
    if raw is None:
        glossary[key] = {"entries": []}
    elif isinstance(raw, dict) and "entries" in raw and isinstance(raw["entries"], list):
        # already v2 — keep reference
        pass
    elif isinstance(raw, dict):
        # legacy flat map -> migrate in place to v2
        migrated = []
        unknown = {}
        for ja, en in raw.items():
            if ja == "entries":
                continue
            if not isinstance(en, str) or not ja:
                unknown[ja] = en
                continue
            migrated.append({"ja": str(ja), "en": str(en), "aliases": [], "kind": "character", "note": "v1-migrated"})
        glossary[key] = {"entries": migrated}
        for k, v in unknown.items():
            glossary[key][k] = v
    else:
        glossary[key] = {"entries": []}
    entries = glossary[key]["entries"]
    existing_count = len(entries)
    skipped_full = existing_count >= MAX_PAIRS_PER_SERIES

    def _nx(x):
        return (x or "").strip().lower()

    by_ja = {_nx(e.get("ja")): e for e in entries}
    by_en = {_nx(e.get("en")): e for e in entries}

    seen = {(e.get("ja"), e.get("en")) for e in entries}
    added = 0
    for item in pairs:
        ja, en, aliases = (item if len(item) == 3 else (item[0], item[1], []))
        target = by_ja.get(_nx(ja)) or by_en.get(_nx(en))
        if target is not None:
            have = {_nx(target.get("ja")), _nx(target.get("en"))}
            have |= {_nx(a) for a in (target.get("aliases") or [])}
            fresh = [
                a for a in (aliases or [])
                if a and _nx(a) not in have
            ]
            if fresh:
                target.setdefault("aliases", [])
                target["aliases"].extend(fresh)
                added += len(fresh)
        if (ja, en) in seen:
            continue
        if len(entries) >= MAX_PAIRS_PER_SERIES:
            break
        entries.append({"ja": str(ja), "en": str(en), "aliases": [str(a) for a in (aliases or [])], "kind": "character", "note": ""})
        seen.add((ja, en))
        by_ja[_nx(ja)] = entries[-1]
        by_en[_nx(en)] = entries[-1]
        added += 1
    return key, added, existing_count, skipped_full


# ---------------------------------------------------------------------------
# Library sources (Sonarr / Radarr)
# ---------------------------------------------------------------------------


def sonarr_titles():
    r = requests.get(
        SONARR_URL.rstrip("/") + "/series",
        headers={"X-Api-Key": SONARR_API_KEY},
        timeout=60,
    )
    r.raise_for_status()
    return [s.get("title") for s in r.json() if isinstance(s, dict) and s.get("title")]


def radarr_titles():
    r = requests.get(
        RADARR_URL.rstrip("/") + "/movie",
        headers={"X-Api-Key": RADARR_API_KEY},
        timeout=60,
    )
    r.raise_for_status()
    return [m.get("title") for m in r.json() if isinstance(m, dict) and m.get("title")]


# ---------------------------------------------------------------------------
# AniList disk cache (never caches failures; corruption-safe)
# ---------------------------------------------------------------------------


def cache_key(title):
    """Normalized cache key: lowercase, whitespace-collapsed, ×→x,
    punctuation stripped (same tolerance used for title matching)."""
    return tolerant(title)


def load_cache():
    """Corruption-safe: unparseable/missing file logs a warning and starts
    fresh; never raises."""
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
        print(f"warning: {CACHE_FILE} is not a JSON object; starting fresh", file=sys.stderr)
    except (OSError, ValueError) as e:
        print(f"warning: cannot read {CACHE_FILE} ({e}); starting fresh", file=sys.stderr)
    return {}


def save_cache(cache):
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, CACHE_FILE)
    except OSError as e:
        print(f"warning: cannot write {CACHE_FILE} ({e})", file=sys.stderr)


def cache_lookup(cache, title):
    """Returns (entry, fresh). entry is None on miss; fresh False when the
    entry is stale (past TTL) or its fetched_at is unparseable."""
    entry = cache.get(cache_key(title))
    if not isinstance(entry, dict) or not isinstance(entry.get("characters"), list):
        return None, False
    fresh = False
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
        age_days = (time.time() - fetched.timestamp()) / 86400.0
        fresh = age_days < CACHE_TTL_DAYS
    except (KeyError, TypeError, ValueError):
        fresh = False
    if fresh:
        # entries fetched before alias capture lack the alternative field;
        # treat them as stale so one refetch backfills it
        chars = entry.get("characters") or []
        if not chars or not all(
            isinstance(c, dict) and "alternative" in c for c in chars
        ):
            fresh = False
    return entry, fresh


# ---------------------------------------------------------------------------
# Per-title pipeline
# ---------------------------------------------------------------------------


def anilist_search_str(title):
    """AniList's search tokenizer chokes on × (U+00D7); translate it to 'x'
    for the query only. The glossary key always stays the verbatim title."""
    return title.replace("\u00d7", "x")


def fetch_media(title, is_movie=False):
    """Search AniList; returns media dict or None. Exact romaji/english
    match preferred; falls back to the first result with a warning."""
    search = anilist_search_str(title)
    results = []
    warn = None
    if is_movie:
        try:
            body = anilist_query(
                {"search": search, "format": "MOVIE"}, query=SEARCH_QUERY_FORMAT
            )
            media = (body or {}).get("data", {}).get("Media")
            if media:
                results.append(media)
        except RuntimeError:
            pass
        if not results:
            warn = "no MOVIE-format match"
    try:
        body = anilist_query({"search": search, "id": None})
        media = (body or {}).get("data", {}).get("Media")
        if media:
            results.append(media)
    except RuntimeError as e:
        if not results:
            raise
    if not results:
        return None, warn or "no AniList match"
    norm_t = norm(title)
    tol_t = tolerant(title)
    matches = [
        m
        for m in results
        if norm(m.get("title", {}).get("romaji")) == norm_t
        or norm(m.get("title", {}).get("english")) == norm_t
        or tolerant(m.get("title", {}).get("romaji")) == tol_t
        or tolerant(m.get("title", {}).get("english")) == tol_t
    ]
    if not matches:
        if len(results) > 1:
            other = ", ".join(
                (m.get("title", {}).get("romaji") or "?") for m in results[:3]
            )
            return results[0], f"no exact title match, using first result ({other})"
        return results[0], "no exact title match, using first result"
    if len(matches) > 1:
        other = ", ".join(
            (m.get("title", {}).get("romaji") or "?") for m in matches[1:3]
        )
        return matches[0], f"ambiguous title ({other}); using first match"
    return matches[0], None


def fetch_characters(media):
    chars = []
    for edge in (media.get("characters", {}) or {}).get("edges", []) or []:
        name = ((edge or {}).get("node", {}) or {}).get("name", {}) or {}
        chars.append(
            {
                "role": (edge or {}).get("role"),
                "full": name.get("full"),
                "native": name.get("native"),
                "alternative": [
                    a for a in (name.get("alternative") or []) if a
                ],
            }
        )
    return chars


def resolve_characters(cache, title, is_movie=False):
    """Returns (chars, source): chars from the disk cache when fresh,
    otherwise from AniList (and cached on success). source is 'cache' or
    'api'; on failure returns (None, reason-string)."""
    entry, fresh = cache_lookup(cache, title)
    if entry and fresh:
        return entry["characters"], "cache"
    if entry:
        print(f"[cache] stale {title} (refreshing)")
    media, warn = fetch_media(title, is_movie=is_movie)
    if media is None:
        return None, warn
    chars = fetch_characters(media)
    if not chars:
        return None, "AniList returned no characters"
    cache[cache_key(title)] = {
        "fetched_at": datetime.now().isoformat(),
        "media_id": media.get("id"),
        "title_romaji": ((media.get("title", {}) or {}).get("romaji") or ""),
        "characters": chars,
    }
    save_cache(cache)
    return chars, "api"


def process_title(cache, glossary, title, is_movie=False):
    """Fetch (cache-first) + merge one title; returns a summary line dict."""
    # glossary-as-cache: series already at the ref cap -> no API call at all,
    # UNLESS its entries still lack aliases (alias enrichment needs one fetch)
    key = find_existing_key(glossary, title) or title
    series = glossary.get(key)
    entries_now = []
    if isinstance(series, dict) and isinstance(series.get("entries"), list):
        entries_now = series["entries"]
    elif isinstance(series, dict):
        entries_now = [
            {"ja": k, "en": v} for k, v in series.items()
            if k != "entries" and isinstance(v, str)
        ]
    if len(entries_now) >= MAX_PAIRS_PER_SERIES and all(
        not (e.get("aliases") if isinstance(e, dict) else None) for e in entries_now
    ):
        return {
            "title": title,
            "warn": f"glossary already full ({len(entries_now)} refs); skipped",
            "ok": False,
        }
    chars, src = resolve_characters(cache, title, is_movie=is_movie)
    if src != "cache" and src != "api":
        return {"title": title, "warn": src, "ok": False}
    print(f"[cache] {'hit' if src == 'cache' else 'miss'} {title}")
    pairs = build_pairs(chars)
    aliases_before = sum(
        len(e.get("aliases") or []) for e in entries_now if isinstance(e, dict)
    )
    key, added, existing_count, skipped_full = merge_pairs(glossary, title, pairs)
    series_after = glossary.get(key)
    entries_after = []
    if isinstance(series_after, dict) and isinstance(series_after.get("entries"), list):
        entries_after = series_after["entries"]
    elif isinstance(series_after, dict):
        entries_after = [
            {"ja": k2, "en": v} for k2, v in series_after.items()
            if k2 != "entries" and isinstance(v, str)
        ]
    aliases_after = sum(
        len(e.get("aliases") or []) for e in entries_after if isinstance(e, dict)
    )
    changed = (
        len(entries_after) != len(entries_now)
        or aliases_after != aliases_before
    )
    media_title = (cache.get(cache_key(title), {}) or {}).get("title_romaji") or "?"
    if skipped_full and not changed:
        return {
            "title": title,
            "anilist": media_title,
            "warn": f"series already at {existing_count} refs; nothing added",
            "ok": False,
        }
    return {
        "title": title,
        "anilist": media_title,
        "key": key,
        "existing": existing_count,
        "added": added,
        "aliases_added": aliases_after - aliases_before,
        "candidates": len(pairs),
        "ok": True,
    }


def write_glossary(glossary, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(glossary, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def upgrade_glossary_v2(path):
    """Convert an existing glossary.json to schema v2 in place.

    Writes a backup ``path.bak-v1`` first. Migrated entries get note
    ``v1-migrated``. Unknown top-level and per-series keys are preserved.
    Already-v2 series are left intact (normalized). Never performs network
    fetches.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        print(f"upgrade: {path} not found, nothing to do")
        return 0
    except (OSError, ValueError) as e:
        print(f"upgrade: cannot read {path}: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"upgrade: {path} is not a JSON object", file=sys.stderr)
        sys.exit(1)
    # backup
    bak = path + ".bak-v1"
    try:
        with open(bak, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError as e:
        print(f"upgrade: cannot write backup {bak}: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"upgrade: wrote backup {bak}")
    new_data = {}
    for series, raw in data.items():
        if not isinstance(raw, dict):
            new_data[series] = raw
            continue
        if "entries" in raw and isinstance(raw["entries"], list):
            # already v2 — normalize entries, preserve unknown per-series keys
            unknown = {k: v for k, v in raw.items() if k != "entries"}
            entries = []
            for e in raw["entries"]:
                if not isinstance(e, dict) or not e.get("ja") or not e.get("en"):
                    continue
                entries.append({
                    "ja": str(e.get("ja")),
                    "en": str(e.get("en")),
                    "aliases": list(e.get("aliases") or []) if isinstance(e.get("aliases"), list) else ([str(e.get("aliases"))] if e.get("aliases") else []),
                    "kind": e.get("kind") if e.get("kind") in ("character", "place", "term") else "character",
                    "note": str(e.get("note") or ""),
                })
            new_data[series] = {"entries": entries}
            for k, v in unknown.items():
                new_data[series][k] = v
        else:
            # legacy flat map -> v2
            entries = []
            unknown = {}
            for ja, en in raw.items():
                if ja == "entries":
                    continue
                if not isinstance(en, str) or not ja:
                    unknown[ja] = en
                    continue
                entries.append({"ja": str(ja), "en": str(en), "aliases": [], "kind": "character", "note": "v1-migrated"})
            new_data[series] = {"entries": entries}
            for k, v in unknown.items():
                new_data[series][k] = v
    write_glossary(new_data, path)
    print(f"upgrade: wrote v2 {path} ({len(new_data)} series)")
    return 0


# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="AniList glossary fetcher")
    ap.add_argument("--series", metavar="TITLE", help="fetch one title (used verbatim as glossary key)")
    ap.add_argument("--movie", action="store_true", help="with --series: treat the title as a movie (MOVIE-format AniList search first)")
    ap.add_argument("--all", action="store_true", help="fetch every Sonarr/Radarr title missing from the glossary")
    ap.add_argument("--glossary", default=GLOSSARY_FILE, help="glossary.json path")
    ap.add_argument("--upgrade-v2", action="store_true", help="convert glossary.json to schema v2 in place (writes .bak-v1, no network fetch)")
    args = ap.parse_args()
    if args.upgrade_v2:
        if args.series or args.all:
            ap.error("--upgrade-v2 cannot be combined with --series or --all")
        sys.exit(upgrade_glossary_v2(args.glossary))
    if args.series and args.all:
        ap.error("use either --series or --all, not both")
    if not args.series and not args.all:
        ap.error("nothing to do: pass --series TITLE or --all or --upgrade-v2")

    glossary = load_glossary(args.glossary)
    print(f"glossary: {args.glossary} ({len(glossary)} series loaded)")
    cache = load_cache()
    print(f"cache: {CACHE_FILE} ({len(cache)} entries)")

    summaries = []
    if args.series:
        summaries.append(process_title(cache, glossary, args.series, is_movie=args.movie))
    else:
        try:
            titles = []
            for t in sonarr_titles():
                titles.append((t, False))
            for t in radarr_titles():
                titles.append((t, True))
            # dedupe by normalized title
            seen = set()
            unique = []
            for t, is_movie in titles:
                key = norm(t)
                if key in seen:
                    continue
                seen.add(key)
                unique.append((t, is_movie))
            missing = [(t, m) for t, m in unique if find_existing_key(glossary, t) is None]
            print(
                f"library: {len(unique)} unique titles, {len(missing)} missing from glossary"
            )
            for t, m in missing:
                summaries.append(process_title(cache, glossary, t, is_movie=m))
        except requests.RequestException as e:
            print(f"library scan failed: {e}", file=sys.stderr)
            sys.exit(1)

    if any(s.get("ok") for s in summaries):
        write_glossary(glossary, args.glossary)
        print(f"wrote {args.glossary}")

    print("\nsummary:")
    for s in summaries:
        if s.get("ok"):
            extra = f" (candidates {s['candidates']})" if s.get("candidates") else ""
            print(
                f"  [added] {s['title']}: +{s['added']} pairs (existing {s['existing']}, key '{s['key']}', AniList '{s['anilist']}'){extra}"
            )
        else:
            print(f"  [skip ] {s['title']}: {s.get('warn', 'no change')}")
    print(f"\nAniList API calls: {_api_calls}")


if __name__ == "__main__":
    main()
