#!/usr/bin/env python3
"""Compare generic Gemma JSON prompt variants on the live local server.

All variants use the generic JSON-array contract with KNOWLEDGE injection.

Scores: parsed count, CJK echo entries, name correctness (Issei/Asia/Azazel/Rizevim
present, drift forms absent), KNOWLEDGE leakage in output.

Offline assertions verify knowledge_block() shape without a live server.
Live llama-server calls are guarded: if unreachable, they are skipped cleanly.
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

GLOSS = [
    ("アーシア", "Asia"),
    ("イッセー", "Issei"),
    ("ディオドラ", "Diodora"),
    ("アザゼル", "Azazel"),
    ("リアス", "Rias"),
    ("ゼノヴィア", "Xenovia"),
    ("リゼヴィム", "Rizevim"),
]

LINES = [
    "アーシアを助けたいんだ。",
    "イッセー、待ってくれ。",
    "ディオドラはどこにいる？",
    "アザゼル先生が言ってた。",
    "リアス先輩はもう知ってる。",
    "ゼノヴィアが笑った。",
    "リゼヴィムが動き出した。",
    "俺たちは負けない。",
    "それが俺の答えだ。",
]


def _offline_assertions():
    entries = [{"ja": ja, "en": en, "aliases": [], "kind": "character", "note": ""} for ja, en in GLOSS]
    block = gl.knowledge_block("ProbeSeries", entries=entries)
    assert "=>" in block, block
    assert "REF:" not in block, block
    assert "official" in block.lower(), block
    assert "KNOWLEDGE" in block, block
    # via temp glossary file
    d = tempfile.mkdtemp()
    path = os.path.join(d, "glossary.json")
    fixture = {"ProbeSeries": {"entries": entries}}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fixture, fh, ensure_ascii=False)
    saved = gl.GLOSSARY_FILE
    gl.GLOSSARY_FILE = path
    gl.reset_cache()
    try:
        b2 = gl.knowledge_block("ProbeSeries")
        assert "=>" in b2 and "REF:" not in b2, b2
    finally:
        gl.GLOSSARY_FILE = saved
        gl.reset_cache()
    print("offline knowledge_block assertions: OK")


def name_score(text):
    good = [n for n in ("Issei", "Asia", "Azazel", "Rizevim", "Diodora", "Rias", "Xenovia") if n in text]
    drift = [n for n in ("Issai", "Arshia", "Azazal", "Lizevim", "Dio德拉") if n in text]
    return good, drift


def _knowledge_for_gloss():
    entries = [{"ja": ja, "en": en, "aliases": [], "kind": "character", "note": ""} for ja, en in GLOSS]
    return gl.knowledge_block("ProbeSeries", entries=entries)


def mode_ref_lines():
    knowledge = _knowledge_for_gloss()
    system, user = o._gemma_prompt(LINES, "Indonesian", refs=knowledge)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def mode_official_terminology(variant="zh"):
    # Both variants now use the same KNOWLEDGE block (terminology intervention
    # retired); kept as separate entry points for parity.
    knowledge = _knowledge_for_gloss()
    system, user = o._gemma_prompt(LINES, "Indonesian", refs=knowledge)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def mode_official_format():
    system, user = o._gemma_prompt(LINES, "Indonesian")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def call(messages):
    return o.post_chat(CFG, messages, CFG["TRANSLATE_MODEL"], "probe", local=True)


def evaluate(name, messages, n_expect, mode):
    try:
        raw = call(messages)
    except Exception as e:
        print(f"{name}: NETWORK FAIL ({e}) — skipping live")
        return
    if raw is None:
        print(f"{name}: NETWORK FAIL — skipping live (llama-server not reachable)")
        return
    parsed = o._parse_local_response(raw, n_expect)
    p = len(parsed) if parsed else 0
    cjk = sum(1 for k in range(1, n_expect + 1) if parsed and parsed.get(k) and CJK.search(parsed[k]))
    leak = "KNOWLEDGE" in raw and "official names" in raw and p != n_expect
    good, drift = name_score(raw)
    ok = p == n_expect and cjk == 0 and not drift
    # knowledge should not be echoed as a directive list
    if "REF:" in raw:
        ok = False
    print(
        f"{name}: parsed={p}/{n_expect} cjk={cjk} leak={leak} "
        f"names={good} drift={drift} -> {'OK' if ok else 'FAIL'}"
    )
    if not ok:
        print("   raw:", raw[:400].replace("\n", " | "))


def main():
    try:
        _offline_assertions()
    except AssertionError as e:
        print(f"offline assertions: FAIL ({e})")
        return 1

    n = len(LINES)
    print("=== baseline: KNOWLEDGE block + JSON ===")
    evaluate("ref_lines", mode_ref_lines(), n, "json")
    print("=== official terminology (zh template) — now KNOWLEDGE ===")
    evaluate("term_zh", mode_official_terminology("zh"), n, "json")
    print("=== official terminology (id template) — now KNOWLEDGE ===")
    evaluate("term_id", mode_official_terminology("id"), n, "json")
    print("=== generic JSON prompt (no knowledge) ===")
    evaluate("format_s", mode_official_format(), n, "json")
    print("live probe done (skipped parts above if no server)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
