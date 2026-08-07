#!/usr/bin/env python3
"""Probe the live HY-MT1.5-7B (llama-server :8011) for glossary terminology.

Sends a numbered chunk with 0-15 glossary terms via HY-MT1.5's official
terminology-intervention template (report 3.4, Scenario 1) and checks:
  - output parseable as numbered list (tolerant N. N． N… parser)
  - no CJK echo in first 10 entries
  - terminology lines not echoed back verbatim in the reply
Run from the PC: /home/user/benchmark/venvs/fw/bin/python pipeline/probe_refs.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orchestrator as o

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")

CFG = {
    "TRANSLATE_BASE": "http://127.0.0.1:8011/v1",
    "TRANSLATE_MODEL": "HY-MT1.5-7B-Q4_K_M.gguf",
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


def probe(n_refs, lines):
    pairs = GLOSS_15[:n_refs]
    n = len(lines)
    system = (
        f"Translate each line into Indonesian. Reply as numbered list, "
        f"e.g. 1. ... 2. ... 3. ..., exactly {n} lines, no extra text."
    )
    if pairs:
        term = "；".join(f"{j}翻译成{i}" for j, i in pairs)
        system += "\n\n参考下面的翻译：\n" + term
    prompt = system + "\n\n" + "\n".join(f"{i}. {l}" for i, l in enumerate(lines, 1))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    raw = o.post_chat(CFG, messages, CFG["TRANSLATE_MODEL"], "probe", local=True)
    if raw is None:
        return {"refs": n_refs, "raw": None}
    parsed = o._parse_numbered_response(raw)
    cjk_entries = []
    for k in range(1, min(11, n) + 1):
        t = parsed.get(k, "")
        if t and CJK.search(t):
            cjk_entries.append(k)
    echoed_refs = [l for l in ("参考下面的翻译", "翻译成") if l in raw]
    return {
        "refs": n_refs,
        "parsed": len(parsed) if parsed else 0,
        "want": n,
        "raw": raw,
        "cjk_entries": cjk_entries,
        "echo_ref_names": echoed_refs,
        "raw_tail": raw[-120:] if raw else None,
    }


def main():
    for n_refs in (0, 5, 10, 15):
        r = probe(n_refs, LINES)
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
