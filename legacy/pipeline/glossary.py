"""Per-series name glossary for translation prompts.

Config file: ~/.config/asr-pipeline/glossary.json
Schema v2: {"<Series>": {"entries": [{"ja": str, "en": str, "aliases": [str], "kind": "character"|"place"|"term", "note": str}]}}
Legacy flat map {"<Series>": {"カナ": "En"}} is accepted by migrating IN MEMORY
(each pair -> entry with ja=<kana key>, en=<value>, kind="character",
note="v1-migrated"); the file is never rewritten implicitly.

Injected into local Gemma JSON translation prompts as KNOWLEDGE background
to fix romanization drift (Issai/Arshia/Azazal -> Issei/Asia/Azazel).
Max 15 refs per series.
"""

import json
import os

GLOSSARY_FILE = os.environ.get(
    "GLOSSARY_FILE",
    os.path.join(os.path.expanduser("~"), ".config", "asr-pipeline", "glossary.json"),
)
MAX_REFS = 15

_cache = None


def load_glossary(path=None):
    global _cache
    if _cache is not None:
        return _cache
    path = path or GLOSSARY_FILE
    data = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            data = raw
    except (OSError, ValueError):
        pass
    _cache = data
    return data


def reset_cache():
    global _cache
    _cache = None


def _norm(s):
    return " ".join((s or "").strip().lower().split())


def _find_key(gloss, series_title):
    if not series_title:
        return None
    norm = _norm(series_title)
    for k in gloss:
        if _norm(k) == norm:
            return k
    if norm:
        for k in gloss:
            if _norm(k) in norm:
                return k
    return None


def _entries_from_raw(raw):
    """Normalize a raw glossary value to v2 entry list.

    v2: {"entries": [{ja, en, aliases, kind, note}, ...]}
    v1 legacy flat map: {"カナ": "En", ...} -> migrated in memory.
    Unknown keys beside 'entries' are ignored here (preserved by upgrade tool).
    """
    if not isinstance(raw, dict):
        return []
    # v2 path: dict with 'entries' list
    if "entries" in raw and isinstance(raw["entries"], list):
        out = []
        for e in raw["entries"]:
            if not isinstance(e, dict):
                continue
            ja = e.get("ja")
            en = e.get("en")
            if not ja or not en:
                continue
            aliases = e.get("aliases") or []
            if not isinstance(aliases, list):
                aliases = [str(aliases)]
            else:
                aliases = [str(a) for a in aliases if a]
            kind = e.get("kind") or "character"
            if kind not in ("character", "place", "term"):
                kind = "character"
            note = e.get("note") or ""
            out.append({"ja": str(ja), "en": str(en), "aliases": aliases, "kind": kind, "note": str(note)})
        return out
    # legacy flat map: each key -> string value is an entry
    out = []
    for ja, en in raw.items():
        if not ja or not en:
            continue
        if not isinstance(en, str):
            continue
        # skip non-entry meta keys that could appear in mixed files
        if ja in ("entries",):
            continue
        out.append({"ja": str(ja), "en": str(en), "aliases": [], "kind": "character", "note": "v1-migrated"})
    return out


def knowledge_entries(series_title, max_refs=MAX_REFS):
    """v2 entry list for a series; migrates v1 flat-map entries in memory.

    Returns [] when the series has no glossary entry. Sliced to max_refs.
    """
    if not series_title:
        return []
    gloss = load_glossary()
    if not gloss:
        return []
    key = _find_key(gloss, series_title)
    if key is None:
        return []
    raw = gloss.get(key)
    entries = _entries_from_raw(raw)
    if max_refs is not None:
        entries = entries[:max_refs]
    return entries


def knowledge_block(series_title, max_refs=MAX_REFS, entries=None):
    """Rendered KNOWLEDGE block, "" when empty.

    When entries is provided, it is used directly (already resolved/filtered);
    otherwise knowledge_entries() is called. The block reads as background
    knowledge, never as a replace-directive list.
    """
    if entries is None:
        entries = knowledge_entries(series_title, max_refs=max_refs)
    if not entries:
        return ""
    lines = [
        "KNOWLEDGE \u2014 official names for this series. When a Japanese form below appears (including aliases), translate it as its official English name:"
    ]
    for e in entries:
        ja = e.get("ja", "")
        en = e.get("en", "")
        aliases = e.get("aliases") or []
        kind = e.get("kind") or "character"
        if aliases:
            alias_str = "; ".join(aliases)
            lines.append(f"- {ja} => {en} (aliases: {alias_str}; {kind})")
        else:
            lines.append(f"- {ja} => {en} ({kind})")
    return "\n".join(lines)


def matched_entries(series_title, cue_texts, max_refs=MAX_REFS):
    """Episode cast resolution: scan cue texts for any entry surface form.

    Case-insensitive substring search over ja, en, aliases. Returns only
    matched entries; if fewer than 3 match, returns the FULL series entry
    list (ASR-garbling fallback). Empty series -> [].
    """
    entries = knowledge_entries(series_title, max_refs=max_refs)
    if not entries:
        return []
    if not cue_texts:
        return entries
    if isinstance(cue_texts, (list, tuple)):
        haystack = "\n".join(str(t) for t in cue_texts).lower()
    else:
        haystack = str(cue_texts).lower()
    if not haystack.strip():
        return entries
    matched = []
    for e in entries:
        surfaces = [e.get("ja", ""), e.get("en", "")] + list(e.get("aliases") or [])
        for s in surfaces:
            if s and s.lower() in haystack:
                matched.append(e)
                break
    if len(matched) < 3:
        return entries
    return matched


def knowledge_block_for_cues(series_title, cue_texts, max_refs=MAX_REFS):
    """KNOWLEDGE block filtered to episode cast, with <3 fallback to full list."""
    entries = matched_entries(series_title, cue_texts, max_refs=max_refs)
    if not entries:
        return ""
    # reuse knowledge_block rendering but with pre-resolved entries
    return knowledge_block(series_title, max_refs=max_refs, entries=entries)
