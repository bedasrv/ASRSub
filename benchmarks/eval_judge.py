#!/usr/bin/env python3
"""LLM judge for translation quality.

Two modes:
  --mode mt    : judge the FLORES sample MT output (Indonesian) against the
                 Japanese source: naturalness 1-5 + fidelity 1-5 (50-line sample).
  --mode srt   : judge the real pipeline output: /tmp/DxD_S03E07.test.id.srt
                 (Indonesian) vs /tmp/full_cues.json (Japanese, parallel by
                 index): naturalness + faithfulness, 30 random cues.

Judge backend: TRANSLATE_API_KEY from ~/.config/asr-pipeline/pipeline.env
(never printed) via https://api.opencode.ai/zen/v1/chat/completions (deepseek
gateway); falls back to https://opencode.ai/zen/go/v1, then to the local
llama-server :8011 using the current Gemma model if the API is unreachable.
"""

import argparse
import json
import os
import random
import re
import sys
import time
import unicodedata

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.expanduser("~/.config/asr-pipeline/pipeline.env")
RESULTS_DIR = os.path.join(HERE, "results")
LOCAL_BASE = "http://127.0.0.1:8011/v1"
LOCAL_MODEL = "/home/user/Documents/Tools/llama-cpp-turboquant/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q6_K_P.gguf"

# task-specified endpoint first, then the verified working zen route
ZEN_URLS = [
    "https://api.opencode.ai/zen/v1/chat/completions",
    "https://opencode.ai/zen/go/v1/chat/completions",
]
ZEN_MODEL = "deepseek-v4-flash"


def load_key():
    key = ""
    with open(ENV_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("TRANSLATE_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key


class Judge:
    def __init__(self):
        self.key = load_key()
        self.backend = "none"

    def _call(self, url, payload, timeout=420):
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {(resp.text or '')[:120]}")
        return resp.json()

    def _post_zen(self, messages, timeout=420):
        payload = {
            "model": ZEN_MODEL,
            "messages": messages,
            "temperature": 0.1,
            "thinking": {"type": "enabled", "effort": "max"},
            "max_tokens": 8192,
        }
        last = None
        for url in ZEN_URLS:
            try:
                data = self._call(url, payload, timeout)
                self.backend = url
                return data["choices"][0]["message"]["content"]
            except Exception as e:
                last = e
        raise RuntimeError(f"zen API unreachable: {last!r}")

    def _post_local(self, messages, timeout=420):
        payload = {
            "model": LOCAL_MODEL,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 8192,
        }
        for attempt in range(5):
            try:
                data = self._call(LOCAL_BASE + "/chat/completions", payload, timeout)
                self.backend = LOCAL_BASE
                return data["choices"][0]["message"]["content"]
            except Exception as e:
                wait = 10 * (attempt + 1)
                print(f"  local retry {attempt + 1} after {e!r}, sleeping {wait}s")
                time.sleep(wait)
        raise RuntimeError("local llama-server unreachable after retries")

    def judge_batch(self, items, task_desc, local=False):
        """items: list of dicts {id, src, hyp, lang}. Returns list of
        {id, naturalness, fidelity, note}."""
        sys_prompt = (
            "You are a translation quality judge for an anime subtitle pipeline. "
            "Japanese is the source language (ground truth). Score each translated "
            "line on two axes, 1-5 (5 = best):\n"
            "- naturalness: is the target-language text fluent, idiomatic and natural?\n"
            "- fidelity: does it faithfully convey the Japanese meaning (no omissions, "
            "additions, mistranslations or name errors)?\n"
            f"Task: {task_desc}\n"
            'Reply with a JSON array only, one object per line: '
            '[{"i": <line number as given>, "naturalness": <int 1-5>, '
            '"fidelity": <int 1-5>, "note": "<one short phrase>"}, ...]'
        )
        lines = []
        for it in items:
            lines.append(
                f'--- line {it["id"]} ---\n'
                f'[source {it["src_lang"]}] {it["src"]}\n'
                f'[target {it["hyp_lang"]}] {it["hyp"]}'
            )
        user = "\n\n".join(lines)
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user},
        ]
        if local:
            content = self._post_local(messages)
        else:
            content = self._post_zen(messages)
        if content is None:
            return None
        arr = re.search(r"\[.*\]", content, re.S)
        if not arr:
            return None
        try:
            return json.loads(arr.group(0))
        except Exception:
            return None


def parse_srt_cues(path):
    lines = [l.strip() for l in open(path, encoding="utf-8")]
    cues = []
    i = 0
    while i < len(lines):
        if re.match(r"^\d+$", lines[i]) and i + 1 < len(lines) and "-->" in lines[i + 1]:
            j = i + 2
            txt = []
            while j < len(lines) and not re.match(r"^\d+$", lines[j]) and "-->" not in lines[j]:
                txt.append(lines[j])
                j += 1
            cues.append(" ".join(txt).strip())
            i = j
        else:
            i += 1
    return cues


def norm(s):
    return " ".join(unicodedata.normalize("NFKC", s).split())


def mode_mt(args):
    sample_dir = os.path.join(HERE, "data", "flores_sample")
    sys_path = os.path.join(RESULTS_DIR, "mt_hy_mt15_flores200.sys.txt")
    with open(os.path.join(sample_dir, "devtest.sample.ja.txt"), encoding="utf-8") as fh:
        src = [l.rstrip("\n") for l in fh]
    with open(sys_path, encoding="utf-8") as fh:
        sys_lines = [l.rstrip("\n") for l in fh]
    assert len(src) == len(sys_lines), f"{len(src)} vs {len(sys_lines)}"
    rng = random.Random(args.seed)
    idxs = sorted(rng.sample(range(len(src)), min(args.n, len(src))))
    items = [{"id": i, "src": src[i], "hyp": sys_lines[i],
              "src_lang": "ja", "hyp_lang": "id"} for i in idxs]
    return items, "FLORES-200 devtest MT output (Japanese -> Indonesian), 50-line sample"


def mode_srt(args):
    cues_ja = json.load(open("/tmp/full_cues.json", encoding="utf-8"))
    cues_id = parse_srt_cues("/tmp/DxD_S03E07.test.id.srt")
    assert len(cues_ja) == len(cues_id), f"{len(cues_ja)} vs {len(cues_id)}"
    rng = random.Random(args.seed)
    idxs = sorted(rng.sample(range(len(cues_ja)), min(args.n, len(cues_ja))))
    items = [{"id": i, "src": cues_ja[i]["text"], "hyp": cues_id[i],
              "src_lang": "ja", "hyp_lang": "id"} for i in idxs]
    return items, ("real pipeline output DxD S03E07 (ASR Japanese -> translated "
                   "Indonesian SRT), 30 random cues")


def report(items, scored, task_desc, out_name, mean_out):
    sc = {}
    for s in scored:
        sc[s["i"]] = s
    pairs = []
    for it in items:
        s = sc.get(it["id"])
        pairs.append((it, s))

    def mean(field):
        vals = [s[field] for _, s in pairs if s and isinstance(s.get(field), (int, float))]
        return sum(vals) / len(vals) if vals else None

    nat_mean = mean("naturalness")
    fid_mean = mean("fidelity")

    worst_nat = sorted([p for p in pairs if p[1] and isinstance(p[1].get("naturalness"), (int, float))],
                       key=lambda p: (p[1]["naturalness"], p[1].get("fidelity", 5)))
    worst_fid = sorted([p for p in pairs if p[1] and isinstance(p[1].get("fidelity"), (int, float))],
                       key=lambda p: (p[1]["fidelity"], p[1].get("naturalness", 5)))

    result = {
        "task": task_desc,
        "n": len(items),
        "backend": judge.backend,
        "naturalness_mean": round(nat_mean, 2) if nat_mean else None,
        "fidelity_mean": round(fid_mean, 2) if fid_mean else None,
        "worst3_naturalness": [
            {"i": p[0]["id"], "src": p[0]["src"], "hyp": p[0]["hyp"], **p[1]}
            for p in worst_nat[:3] if p[1]
        ],
        "worst3_fidelity": [
            {"i": p[0]["id"], "src": p[0]["src"], "hyp": p[0]["hyp"], **p[1]}
            for p in worst_fid[:3] if p[1]
        ],
        "lines": [
            {"i": p[0]["id"], "src": p[0]["src"], "hyp": p[0]["hyp"],
             "naturalness": (p[1] or {}).get("naturalness"),
             "fidelity": (p[1] or {}).get("fidelity"),
             "note": (p[1] or {}).get("note")}
            for p in pairs
        ],
    }
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, out_name), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print("=" * 60)
    print(task_desc)
    print(f"backend: {judge.backend}")
    print(f"n = {len(items)}")
    print(f"mean naturalness: {result['naturalness_mean']}")
    print(f"mean fidelity:    {result['fidelity_mean']}")
    print("-- worst 3 by naturalness --")
    for p in worst_nat[:3]:
        print(f"  line {p[0]['id']}: nat={p[1]['naturalness']} fid={p[1]['fidelity']} | {p[0]['hyp'][:60]}")
    print("-- worst 3 by fidelity --")
    for p in worst_fid[:3]:
        print(f"  line {p[0]['id']}: nat={p[1]['fidelity']} fid={p[1]['naturalness']} | {p[0]['hyp'][:60]}")
    print(f"saved: {os.path.join(RESULTS_DIR, out_name)}")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["mt", "srt"], required=True)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--batch", type=int, default=5, help="lines per judge request")
    ap.add_argument("--local", action="store_true", help="force local llama-server judge")
    args = ap.parse_args()

    global judge
    judge = Judge()
    if not judge.key:
        print("WARNING: TRANSLATE_API_KEY not found; will use local llama-server only",
              file=sys.stderr)

    if args.mode == "mt":
        items, desc = mode_mt(args)
        out_name = "judge_mt_flores50.json"
    else:
        items, desc = mode_srt(args)
        out_name = "judge_srt_dxd30.json"

    scored = []
    for b in range(0, len(items), args.batch):
        batch = items[b : b + args.batch]
        s = None
        if not args.local:
            try:
                s = judge.judge_batch(batch, desc)
            except Exception as e:
                print(f"zen API failed ({e!r}); falling back to local llama-server", file=sys.stderr)
        if s is None:
            s = judge.judge_batch(batch, desc, local=True)
        if s is None:
            print(
                f"WARNING: batch {b} unscored — both zen and local judge failed",
                file=sys.stderr,
            )
            continue
        scored.extend(s)
        print(f"scored {len(scored)}/{len(items)} (backend {judge.backend})", flush=True)
        time.sleep(0.5)

    if not scored:
        print("no batches scored — judge backends failed", file=sys.stderr)
        return 1

    report(items, scored, desc, out_name, None)


if __name__ == "__main__":
    main()
