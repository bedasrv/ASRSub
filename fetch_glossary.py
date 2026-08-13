#!/usr/bin/env python3
"""AniList character-name glossary fetcher for the ASR pipeline.

Fetches per-series character names from AniList and merges kana-reading
keys into ~/.config/asr-pipeline/glossary.json so new shows/movies get
translation terminology without manual hardcoding.

Pipeline contract (pipeline/glossary.py): glossary.json maps
    {"Series Title": {"アーシア": "Asia", ...}}
keys are KANA readings (ASR/SenseVoice never outputs kanji), values are
English/romaji names. terminology_block() emits at most 15 refs per series.

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

Env overrides: SONARR_URL, SONARR_API_KEY, RADARR_URL, RADARR_API_KEY,
GLOSSARY_FILE.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import deque

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

MAX_PAIRS_PER_SERIES = 15  # consumer cap in pipeline/glossary.py

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
        node { name { full native } }
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
        node { name { full native } }
      }
    }
  }
}
"""


def anilist_query(variables, query=SEARCH_QUERY, attempts=5):
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
    """characters: [{role, full, native}...]; returns [(ja, en), ...]
    capped at MAX_PAIRS_PER_SERIES, MAIN characters first."""
    ranked = sorted(
        characters,
        key=lambda c: (ROLE_PRIORITY.get(c.get("role"), 9)),
    )
    pairs = []
    seen = set()
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
        cand = [
            (reading, first),
            (reading, full),
            (short, first),
        ]
        for ja, en in cand:
            if not ja or not en:
                continue
            dup = (ja, en) in seen
            seen.add((ja, en))
            if dup:
                continue
            pairs.append((ja, en))
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
    """Returns (key, added, existing_count, skipped_full)."""
    key = find_existing_key(glossary, title) or title
    entries = glossary.setdefault(key, {})
    existing_count = len(entries)
    added = 0
    skipped_full = existing_count >= MAX_PAIRS_PER_SERIES
    for ja, en in pairs:
        if len(entries) >= MAX_PAIRS_PER_SERIES:
            break
        if ja in entries:
            continue
        entries[ja] = en
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
            }
        )
    return chars


def process_title(glossary, title, is_movie=False):
    """Fetch + merge one title; returns a summary line dict."""
    media, warn = fetch_media(title, is_movie=is_movie)
    if media is None:
        return {"title": title, "warn": warn, "ok": False}
    media_title = (
        media.get("title", {}).get("english")
        or media.get("title", {}).get("romaji")
        or "?"
    )
    chars = fetch_characters(media)
    if not chars:
        return {"title": title, "warn": "AniList returned no characters", "ok": False}
    pairs = build_pairs(chars)
    key, added, existing_count, skipped_full = merge_pairs(glossary, title, pairs)
    if skipped_full:
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
        "candidates": len(pairs),
        "ok": True,
    }


def write_glossary(glossary, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(glossary, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="AniList glossary fetcher")
    ap.add_argument("--series", metavar="TITLE", help="fetch one title (used verbatim as glossary key)")
    ap.add_argument("--all", action="store_true", help="fetch every Sonarr/Radarr title missing from the glossary")
    ap.add_argument("--glossary", default=GLOSSARY_FILE, help="glossary.json path")
    args = ap.parse_args()
    if args.series and args.all:
        ap.error("use either --series or --all, not both")
    if not args.series and not args.all:
        ap.error("nothing to do: pass --series TITLE or --all")

    glossary = load_glossary(args.glossary)
    print(f"glossary: {args.glossary} ({len(glossary)} series loaded)")

    summaries = []
    if args.series:
        summaries.append(process_title(glossary, args.series, is_movie=False))
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
                summaries.append(process_title(glossary, t, is_movie=m))
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


if __name__ == "__main__":
    main()
