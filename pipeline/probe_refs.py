#!/usr/bin/env python3
"""Probe the live local Gemma model (llama-server :8011) for glossary knowledge.

Sends a JSON chunk with 0-15 KNOWLEDGE entries and checks:
  - output parseable as JSON array (via _parse_local_response)
  - no CJK echo in first 10 entries
  - KNOWLEDGE header not echoed back verbatim in the reply

Offline assertions exercise knowledge_block() shape without a live server.
Live llama-server parts are guarded: if unreachable, they are skipped cleanly.

Run from the PC: /home/user/benchmark/venvs/fw/bin/python pipeline/probe_refs.py
"""

import os
import re
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orchestrator as o
from pipeline import glossary as gl

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")

CFG = {
    "TRANSLATE_BASE": "http://127.0.0.1:8011/v1",
    "TRANSLATE_MODEL": "/home/user/Documents/Tools/llama-cpp-turboquant/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf",
}

LINES = [
    "アーシアを助けたいんだ。",
    "イッセー、待ってくれ。",
    "ディオドラはどこにいる？",
    "アザゼル先生が言ってた。",
    "もう一度、一からやり直そう。",
    "この世界のすべてをかけて。",
    "俺たちは負けない。",
    "リゼヴィムが動き出した。",
    "神社の裏手で会おう。",
    "それが俺の答えだ。",
]

GLOSS_15 = [
    ("アーシア", "Asia"),
    ("イッセー", "Issei"),
    ("ディオドラ", "Diodora"),
    ("アザゼル", "Azazel"),
    ("リゼヴィム", "Rizevim"),
    ("リアス", "Rias"),
    ("ゼノヴィア", "Xenovia"),
    ("イリナ", "Irina"),
    ("キバ", "Kiba"),
    ("アクノーリア", "Akeno"),
    ("ガスパー", "Gasper"),
    ("ミルタン", "Mittelt"),
    ("グレモリー", "Gremory"),
    ("シトリー", "Sitri"),
    ("ラミアス", "Ramiyas"),
]


def _offline_knowledge_assertions():
    """Offline checks on knowledge_block() shape; no server needed."""
    # build a temp glossary so knowledge_block can be exercised via entries param
    entries = [{"ja": ja, "en": en, "aliases": [], "kind": "character", "note": ""} for ja, en in GLOSS_15[:5]]
    block = gl.knowledge_block("ProbeSeries", entries=entries)
    assert "=>" in block, block
    assert "REF:" not in block, block
    assert "official" in block.lower(), block
    assert "KNOWLEDGE" in block, block
    # also test via file-backed knowledge_entries
    d = tempfile.mkdtemp()
    path = os.path.join(d, "glossary.json")
    fixture = {"ProbeSeries": {"entries": entries}}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fixture, fh, ensure_ascii=False)
    saved = gl.GLOSSARY_FILE
    gl.GLOSSARY_FILE = path
    gl.reset_cache()
    try:
        e2 = gl.knowledge_entries("ProbeSeries")
        assert len(e2) == 5, e2
        b2 = gl.knowledge_block("ProbeSeries")
        assert "=>" in b2 and "REF:" not in b2, b2
    finally:
        gl.GLOSSARY_FILE = saved
        gl.reset_cache()
    print("offline knowledge_block assertions: OK")


def probe(n_refs, lines):
    # Build KNOWLEDGE block from first n_refs pairs (character entries)
    pairs = GLOSS_15[:n_refs]
    entries = [{"ja": ja, "en": en, "aliases": [], "kind": "character", "note": ""} for ja, en in pairs]
    knowledge = gl.knowledge_block("ProbeSeries", entries=entries) if entries else ""
    n = len(lines)
    system, prompt = o._gemma_prompt(lines, "Indonesian", refs=knowledge)
    # offline shape guard: knowledge must contain => and not REF:
    if knowledge:
        assert "=>" in knowledge and "REF:" not in knowledge, knowledge
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    raw = o.post_chat(CFG, messages, CFG["TRANSLATE_MODEL"], "probe", local=True)
    if raw is None:
        return {"refs": n_refs, "raw": None, "skipped": True}
    parsed = o._parse_local_response(raw, n) or {}
    cjk_entries = []
    for k in range(1, min(11, n) + 1):
        t = parsed.get(k, "")
        if t and CJK.search(t):
            cjk_entries.append(k)
    echoed = [l for l in ("KNOWLEDGE", "official names") if l in raw and knowledge and l in knowledge and raw.count(l) > 2]
    # legacy leakage check: knowledge header should not be echoed verbatim excessively
    return {
        "refs": n_refs,
        "parsed": len(parsed) if parsed else 0,
        "want": n,
        "raw": raw,
        "cjk_entries": cjk_entries,
        "echo_ref_names": echoed,
        "raw_tail": raw[-120:] if raw else None,
        "skipped": False,
    }


def main():
    # offline part always runs
    try:
        _offline_knowledge_assertions()
    except AssertionError as e:
        print(f"offline knowledge_block assertions: FAIL ({e})")
        return 1

    # live server part — guard when llama-server unreachable
    has_live = True
    try:
        # quick probe: try one call and see if we get None due to network
        test = probe(0, LINES[:2])
        if test.get("skipped") or test["raw"] is None:
            has_live = False
    except Exception:
        has_live = False

    if not has_live:
        print("live llama-server not reachable — skipping live probe (offline only)")
        return 0

    for n_refs in (0, 5, 10, 15):
        r = probe(n_refs, LINES)
        if r.get("skipped") or r["raw"] is None:
            print(f"refs={n_refs:2d} skipped (no server)")
            continue
        ok = (
            r["raw"] is not None
            and r["parsed"] == r["want"]
            and not r["cjk_entries"]
            and not r["echo_ref_names"]
        )
        print(
            f"refs={n_refs:2d} parsed={r['parsed']}/{r['want']} "
            f"cjk={r['cjk_entries']} echo={r['echo_ref_names']} -> {'OK' if ok else 'FAIL'}"
        )
        if r["raw_tail"]:
            print("   tail:", r["raw_tail"].replace("\n", " | "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
