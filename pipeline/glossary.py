"""Per-series name glossary for translation prompts.

Config file: ~/.config/asr-pipeline/glossary.json
Format: {"Series Title": {"アーシア": "Asia", "ディオドラ": "Diodora", ...}}
Injected into local (7B) translation prompts using HY-MT1.5's officially
documented TERMINOLOGY INTERVENTION template (Technical Report, section
3.4, Scenario 1: Terminology Translation) to fix romanization drift
(Issai/Arshia/Azazal -> Issei/Asia/Azazel). Max 15 refs per series.
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


def refs_for_series(series_title, max_refs=MAX_REFS):
    """REF lines for a series, e.g. ["REF: アーシア = Asia", ...].
    Matches by normalized exact title, else by key-inside-title containment.
    Returns [] when the series has no glossary entry."""
    return [
        f"REF: {ja} = {name}"
        for ja, name in term_pairs(series_title, max_refs=max_refs)
    ]


def terminology_block(series_title, max_refs=MAX_REFS, verb="翻译成", sep="；"):
    """HY-MT1.5 official terminology-intervention line (report 3.4,
    Scenario 1), e.g. "アーシア翻译成Asia；イッセー翻译成Issei".
    Returns "" when the series has no glossary entry."""
    pairs = term_pairs(series_title, max_refs=max_refs)
    if not pairs:
        return ""
    return sep.join(f"{ja}{verb}{name}" for ja, name in pairs)


def term_pairs(series_title, max_refs=MAX_REFS):
    """(ja, name) pairs for a series; matches by normalized exact title,
    else by key-inside-title containment. Returns [] without glossary."""
    if not series_title:
        return []
    gloss = load_glossary()
    if not gloss:
        return []
    norm = _norm(series_title)
    key = None
    for k in gloss:
        if _norm(k) == norm:
            key = k
            break
    if key is None:
        for k in gloss:
            if norm and _norm(k) in norm:
                key = k
                break
    if key is None:
        return []
    names = gloss.get(key) or {}
    if not isinstance(names, dict):
        return []
    return [
        (ja, name)
        for ja, name in list(names.items())[:max_refs]
        if ja and name
    ]
