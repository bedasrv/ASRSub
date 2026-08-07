#!/usr/bin/env python3
"""Compare HY-MT1.5 official prompt templates vs REF-line approach (live 7B).

Scenario 1 (terminology intervention, report 3.4): official template
  参考下面的翻译：
  {terminology}翻译成{lang}
  将以下文本翻译为{lang}，注意只需要输出翻译后的结果，不要额外解释：
  {source}
Scenario 3 (format translation): <s1>..</s1> per line, output <target>str</target>.
Baseline: current REF: lines in system prompt + numbered-list protocol.

Scores: parsed count, CJK echo entries, name correctness (Issei/Asia/Azazel/Rizevim
present, drift forms absent), REF/template leakage in output.
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


def name_score(text):
    good = [n for n in ("Issei", "Asia", "Azazel", "Rizevim", "Diodora", "Rias", "Xenovia") if n in text]
    drift = [n for n in ("Issai", "Arshia", "Azazal", "Lizevim", "Dio德拉") if n in text]
    return good, drift


def mode_ref_lines():
    n = len(LINES)
    term = "\n".join(f"REF: {j} = {i}" for j, i in GLOSS)
    system = (
        f"Translate each line into Indonesian. Reply as numbered list, "
        f"e.g. 1. ... 2. ... 3. ..., exactly {n} lines, no extra text."
        f"\n\nUse these character names in this series when translating:\n{term}"
    )
    prompt = system + "\n\n" + "\n".join(f"{i}. {l}" for i, l in enumerate(LINES, 1))
    return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]


def mode_official_terminology(variant="zh"):
    term = "；".join(f"{j}翻译成{i}" for j, i in GLOSS)
    if variant == "id":
        term = "；".join(f"{j} diterjemahkan menjadi {i}" for j, i in GLOSS)
    body = (
        "参考下面的翻译：\n"
        f"{term}\n"
        "将以下文本翻译为Indonesian，注意只需要输出翻译后的结果，不要额外解释：\n"
        + "\n".join(LINES)
    )
    return [{"role": "user", "content": body}]


def mode_official_format():
    src = "".join(f"<s{i}>{l}</s{i}>" for i, l in enumerate(LINES, 1))
    body = (
        "将以下<source></source>之间的文本翻译为Indonesian，注意只需要输出翻译后的结果，"
        "不要额外解释，原文中的<sn></sn>标签表示标签内文本包含格式信息，需要在译文中相应的位置尽量保留该标签。"
        "输出格式为：<target>str</target>\n"
        f"<source>{src}</source>"
    )
    return [{"role": "user", "content": body}]


def call(messages):
    return o.post_chat(CFG, messages, CFG["TRANSLATE_MODEL"], "probe", local=True)


def evaluate(name, messages, n_expect, mode):
    raw = call(messages)
    if raw is None:
        print(f"{name}: NETWORK FAIL")
        return
    if mode == "format":
        m = re.search(r"<target>(.*)</target>", raw, re.S)
        tags = re.findall(r"<s(\d+)>(.*?)</s\1>", m.group(1), re.S) if m else []
        entries = [t.strip() for _, t in tags]
        parsed = len(entries)
        cjk = sum(1 for t in entries if CJK.search(t))
        leakage = "参考下面的翻译" in raw or "翻译成" in raw
        good, drift = name_score(raw)
        ok = parsed == n_expect and cjk == 0 and not leakage and not drift
        print(
            f"{name}: parsed={parsed}/{n_expect} cjk={cjk} leak={leakage} "
            f"names={good} drift={drift} -> {'OK' if ok else 'FAIL'}"
        )
        if not ok:
            print("   raw:", raw[:400].replace("\n", " | "))
        return
    if mode == "numbered":
        parsed = o._parse_numbered_response(raw)
        p = len(parsed) if parsed else 0
        cjk = sum(1 for k in range(1, n_expect + 1) if parsed and parsed.get(k) and CJK.search(parsed[k]))
        leak = "REF:" in raw
        good, drift = name_score(raw)
        ok = p == n_expect and cjk == 0 and not leak and not drift
        print(
            f"{name}: parsed={p}/{n_expect} cjk={cjk} leak={leak} "
            f"names={good} drift={drift} -> {'OK' if ok else 'FAIL'}"
        )
        if not ok:
            print("   raw:", raw[:400].replace("\n", " | "))
        return
    # plain: whole reply is the translation block
    cjk = sum(1 for l in raw.splitlines() if CJK.search(l) and l.strip())
    leak = "翻译成" in raw or "参考下面的" in raw
    good, drift = name_score(raw)
    ok = cjk == 0 and not leak and not drift
    print(
        f"{name}: cjk={cjk} leak={leak} names={good} drift={drift} -> {'OK' if ok else 'FAIL'}"
    )
    if not ok:
        print("   raw:", raw[:400].replace("\n", " | "))


def main():
    n = len(LINES)
    print("=== baseline: REF lines + numbered ===")
    evaluate("ref_lines", mode_ref_lines(), n, "numbered")
    print("=== official terminology (zh template) ===")
    evaluate("term_zh", mode_official_terminology("zh"), n, "plain")
    print("=== official terminology (id template) ===")
    evaluate("term_id", mode_official_terminology("id"), n, "plain")
    print("=== official format <s1>..</s1> + <target> ===")
    evaluate("format_s", mode_official_format(), n, "format")
    return 0


if __name__ == "__main__":
    sys.exit(main())
