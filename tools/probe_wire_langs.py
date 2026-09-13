#!/usr/bin/env python3
"""Re-run the `WIRE_ACCEPTED_LANGS` measurement against the live endpoint.

`src/lang.rs` decides whether a code may reach the wire as Whisper's `language`
form field by looking it up in `WIRE_ACCEPTED_LANGS`. That table is a *live
measurement* of one provider, not a published list, so this script makes the
measurement re-runnable from a checkout instead of a prose claim:

* candidate universe: the 100 language codes of openai/whisper's own table
  (`whisper/tokenizer.py`, `LANGUAGES`), pinned to commit
  `86098128c0b4f24f0e2aa2994de830614b474227` (2026-08-31);
* one multipart request per candidate (`model`, `response_format=verbose_json`,
  `language=<code>`, a 1 s silent MP3) — the shape the pipeline itself sends;
* the committed set is READ OUT OF `src/lang.rs`, so this compares against the
  code and never against a copy that can drift;
* exit status 1 when any candidate disagrees with the committed set: a code in
  the set that the endpoint does not answer 200, or a code outside the set that
  it does.

The four `UNREACHABLE_SPELLINGS` are probed as well. They are by design
unreachable from the wire — `normalize_lang` maps each to its canonical code
before any pin decision is taken — so they must never appear in the committed
set; their status is recorded so the table's documentation keeps matching
reality. `UNPROBED_CONTROLS` are the other direction: spellings this pipeline
normalizes away or refuses, probed in round 4 and never sent to the endpoint.
Their check is **bidirectional**: a control is a deviation both when it IS in
the committed set and when the endpoint answers it with HTTP 200 — a refused
spelling is only evidence that the refusal is still needed while it is
actually refused, and `jv` becoming accepted would silently invalidate the
`jv` → `jw` remap's rationale.

The accept set is read out of the `(code, name)` pairs of
`WIRE_ACCEPTED_LANGS` — that table is the one definition of the wire codes and
of Whisper's name for each (the reported-name path reads the same pairs) — so
this script compares against the codes the code itself would send, never
against a copy that can drift.

Usage::

    python3 tools/probe_wire_langs.py               # live: 104 requests
    python3 tools/probe_wire_langs.py --dry-run     # print the plan, no requests
    python3 tools/probe_wire_langs.py --only jw --only ln
    python3 tools/probe_wire_langs.py --out /tmp/wire_langs.csv

The endpoint, model and key come from the providers file (`--providers`, else
`$PROVIDERS_FILE`, else `./asrsub_providers.json`), node `whisper_stt`. The key
is never printed, logged or written to the CSV.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

# --- the candidate universe (openai/whisper `LANGUAGES`, its own order) -----
WHISPER_LANGUAGES = (
    "en", "zh", "de", "es", "ru", "ko", "fr", "ja", "pt", "tr", "pl", "ca", "nl", "ar", "sv",
    "it", "id", "hi", "fi", "vi", "he", "uk", "el", "ms", "cs", "ro", "da", "hu", "ta", "no",
    "th", "ur", "hr", "bg", "lt", "la", "mi", "ml", "cy", "sk", "te", "fa", "lv", "bn", "sr",
    "az", "sl", "kn", "et", "mk", "br", "eu", "is", "hy", "ne", "mn", "bs", "kk", "sq", "sw",
    "gl", "mr", "pa", "si", "km", "sn", "yo", "so", "af", "oc", "ka", "be", "tg", "sd", "gu",
    "am", "yi", "lo", "uz", "fo", "ht", "ps", "tk", "nn", "mt", "sa", "lb", "my", "bo", "tl",
    "mg", "as", "tt", "haw", "ln", "ha", "ba", "jw", "su", "yue",
)

# Spellings a tag or a caller may hold that `normalize_lang` rewrites to a
# canonical code before any wire decision: never sent, never in the set.
UNREACHABLE_SPELLINGS = ("en-US", "pt-BR", "zh-Hant", "tagalog")

# Spellings measured in round 4 and refused by every path (uncertainty markers
# and codes the pipeline renames or rejects): probed only with `--controls`,
# for the record. `jv` is the one that matters here — the alias table now maps
# it to the accepted `jw`, so it must stay outside the set itself.
UNPROBED_CONTROLS = ("ceb", "eo", "jv", "zu", "fil", "tgl", "filipino", "tam", "tel", "slo", "cat", "und", "na", "zz")

# 1 s of silence, 8 kHz mono VBR MP3 (LAME -q:a 9), the smallest audio the
# endpoint was measured to accept.
SILENT_MP3_B64 = (
    "SUQzBAAAAAAAIlRTU0UAAAAOAAADTGF2ZjYxLjcuMTAzAAAAAAAAAAAAAAD/4zjAAAAAAAAAAAAASW5mbwAAAA8AAAAQAAAF"
    "WAA1NTU1NTVDQ0NDQ0NQUFBQUFBeXl5eXl5ra2tra2treXl5eXl5hoaGhoaGlJSUlJSUoaGhoaGhoa+vr6+vr7y8vLy8vMrK"
    "ysrKytfX19fX19fl5eXl5eXy8vLy8vL///////8AAAAATGF2YzYxLjE5AAAAAAAAAAAAAAAAJAKAAAAAAAAABVgIAJWUAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/4xjEAAAAA0gAAAAATEFNRTMuMTAwVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVX/4xjEOwAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVX/4xjEdgAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVX/4xjEsQAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVX/4xjExAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVX/4xjExAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVX/4xjExAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVX/4xjExAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVX/4xjExAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVX/4xjExAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVX/4xjExAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVX/4xjExAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVX/4xjExAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/4xjExAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/4xjExAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/4xjExAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVU="
)

CSV_FIELDS = ("code", "http", "detected", "err", "role")


def committed_set(repo_root):
    """Read WIRE_ACCEPTED_LANGS out of src/lang.rs (the code is the oracle).

    The table lists `(code, name)` pairs — the code is the wire accept set, the
    name is Whisper's own name for it — so the codes are the first element of
    each pair. Parsing the second element too is deliberate: a pair damaged
    such that a code loses its name is a defect this probe should not paper
    over, so the match requires both strings.
    """
    path = os.path.join(repo_root, "src", "lang.rs")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(
        r"const WIRE_ACCEPTED_LANGS: &\[\(&str, &str\)\] = &\[(.*?)\n\];", text, re.S
    )
    if not m:
        sys.exit("cannot find WIRE_ACCEPTED_LANGS in %s" % path)
    pairs = re.findall(r'\(\s*"([a-z0-9]+)"\s*,\s*"([^"]+)"\s*\)', m.group(1))
    if not pairs:
        sys.exit("WIRE_ACCEPTED_LANGS parsed empty from %s" % path)
    codes = [code for code, _ in pairs]
    return set(codes), len(codes)


def load_provider(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    node = doc.get("whisper_stt") or {}
    endpoint = node.get("endpoint") or ""
    model = node.get("model") or ""
    key = node.get("api_key") or ""
    if not key and node.get("key_env"):
        key = os.environ.get(node["key_env"], "")
    missing = [n for n, v in (("endpoint", endpoint), ("model", model), ("api key", key)) if not v]
    if missing:
        sys.exit("providers file %s: whisper_stt missing %s" % (path, ", ".join(missing)))
    return endpoint, model, key


def multipart_body(fields, filename, blob):
    boundary = "----asrsubprobe" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields:
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (boundary, name, value)).encode()
    out += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
            "Content-Type: audio/mpeg\r\n\r\n" % (boundary, filename)).encode()
    out += blob
    out += ("\r\n--%s--\r\n" % boundary).encode()
    return bytes(out), "multipart/form-data; boundary=%s" % boundary


def probe(endpoint, model, key, code, blob):
    """One request. Returns (http_status, detected_language, error_snippet)."""
    body, ctype = multipart_body(
        [("model", model), ("language", code), ("response_format", "verbose_json")],
        "silent1s.mp3",
        blob,
    )
    req = urllib.request.Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", ctype)
    req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
            detected = payload.get("language")
            return resp.status, str(detected) if detected is not None else "", ""
    except urllib.error.HTTPError as e:
        snippet = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
        return e.code, "", snippet
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, "", str(e)[:200]


def recheck(csv_path, committed):
    """Compare a stored CSV against the committed set: no requests, same verdict."""
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    candidates = [r for r in rows if r.get("role") == "candidate"]
    if not candidates:
        sys.exit("%s holds no candidate rows" % csv_path)
    accepted = {r["code"] for r in candidates if r["http"] == "200"}
    deviations = []
    for row in candidates:
        in_set = row["code"] in committed
        if (row["http"] == "200") != in_set:
            deviations.append(
                "%s: recorded HTTP %s but %s the committed set"
                % (row["code"], row["http"], "in" if in_set else "outside")
            )
    for row in rows:
        role = row.get("role")
        if role == "candidate":
            continue
        if row["code"] in committed:
            deviations.append(
                "%s: a %s spelling is in the committed set" % (row["code"], role)
            )
        # Bidirectional: a refused control is only evidence while the endpoint
        # actually refuses it. Recorded as HTTP 200, the refusal it documents
        # (and any remap that relied on it) needs re-measuring.
        if role == "control" and row["http"] == "200":
            deviations.append(
                "%s: a refused control answered HTTP 200 — the endpoint now accepts it; "
                "re-measure the set and the remaps that keep it off the wire" % row["code"]
            )
        # Informational: an unreachable spelling is rewritten before the wire,
        # so its raw status says nothing about what the pipeline sends.
        if role == "unreachable" and row["http"] != "200":
            print(
                "note: unreachable spelling %s recorded HTTP %s (rewritten before the wire; "
                "informational only)" % (row["code"], row["http"])
            )
    missing = sorted(committed - accepted)
    print("csv=%s\ncandidates=%d\naccepted=%d\ncommitted=%d"
          % (csv_path, len(candidates), len(accepted), len(committed)))
    if missing:
        print("committed but unanswered in this CSV: %s" % ", ".join(missing))
    if deviations:
        print("DEVIATIONS:")
        for d in deviations:
            print("  " + d)
        return 1
    print("no deviations: the committed set is exactly the accepted codes in this measurement")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--repo", default=os.path.dirname(here), help="checkout root (holds src/lang.rs)")
    ap.add_argument("--providers", default=os.environ.get("PROVIDERS_FILE") or "asrsub_providers.json")
    ap.add_argument("--out", default=None, help="CSV path (default: wire_langs-<date>.csv in $PWD)")
    ap.add_argument("--only", action="append", default=[], help="probe only these codes (repeatable)")
    ap.add_argument("--controls", action="store_true", help="also probe the round-4 refused spellings (14 more)")
    ap.add_argument(
        "--recheck",
        default=None,
        help="compare an existing CSV with the committed set instead of probing (0 requests)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit without requesting")
    args = ap.parse_args(argv)

    committed, committed_n = committed_set(args.repo)

    if args.recheck:
        return recheck(args.recheck, committed)

    universe = set(WHISPER_LANGUAGES)
    if args.only:
        probes = []
        for code in dict.fromkeys(args.only):
            if code in universe:
                probes.append((code, "candidate"))
            elif code in UNREACHABLE_SPELLINGS:
                probes.append((code, "unreachable"))
            else:
                probes.append((code, "control"))
    else:
        probes = [(c, "candidate") for c in WHISPER_LANGUAGES]
        probes += [(c, "unreachable") for c in UNREACHABLE_SPELLINGS]
        if args.controls:
            probes += [(c, "control") for c in UNPROBED_CONTROLS]

    if args.dry_run:
        print("candidate universe: %d Whisper codes, committed set: %d codes"
              % (len(WHISPER_LANGUAGES), committed_n))
        print("requests: %d" % len(probes))
        return 0

    endpoint, model, key = load_provider(args.providers)
    blob = base64.b64decode(SILENT_MP3_B64)
    out_path = args.out or "wire_langs-%s.csv" % datetime.now(timezone.utc).strftime("%Y%m%d")
    rows, deviations, accepted = [], [], []
    for code, role in probes:
        status, detected, err = probe(endpoint, model, key, code, blob)
        rows.append({"code": code, "http": status, "detected": detected, "err": err, "role": role})
        if role == "candidate":
            in_set = code in committed
            if status == 200:
                accepted.append(code)
            if (status == 200) != in_set:
                deviations.append(
                    "%s: HTTP %s but %s the committed set"
                    % (code, status, "in" if in_set else "outside")
                )
            print("%-8s %s http=%s detected=%s" % (code, "in " if in_set else "out", status, detected))
        else:
            flag = ""
            if code in committed:
                deviations.append("%s: %s spelling must never be in the committed set" % (code, role))
            if status != 200:
                flag = "  (warning: %s spelling not accepted)" % role
            # Bidirectional, the other half: a control that the endpoint now
            # ACCEPTS is a deviation too, not a silent no-op. The refusal it
            # documents (`jv` → `jw`, the Tagalog spellings → `tl`) is what
            # keeps it off the wire, so a 200 means those remaps need
            # re-measuring rather than assuming the answer is still 400.
            if role == "control" and status == 200:
                deviations.append(
                    "%s: a refused control answered HTTP 200 — the endpoint now accepts it; "
                    "re-measure the set and the remaps that keep it off the wire" % code
                )
                flag = "  (warning: refused control now accepted)"
            print("%-8s %s http=%s detected=%s%s" % (code, "--", status, detected, flag))

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)

    missing = sorted(committed - set(accepted))
    print("\nendpoint=%s\nmodel=%s\ndate=%s\nrequests=%d\naccepted=%d\ncsv=%s"
          % (endpoint, model, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             len(rows), len(accepted), out_path))
    if missing and not args.only:
        print("committed but unanswered: %s" % ", ".join(missing))
    if deviations:
        print("DEVIATIONS:")
        for d in deviations:
            print("  " + d)
        return 1
    print("no deviations: the committed set is exactly the codes this endpoint accepts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
