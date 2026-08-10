#!/usr/bin/env python3
"""Dry-run tests for orchestrator v2 merge-aware translation.

Monkeypatches post_chat (no llama-server needed). Scenarios:
  - merge: model returns 7 entries for a 10-line chunk (tail completion fills 8-10)
  - echo-then-recover: first attempt echoes CJK, corrective retries succeed
  - tail-completion: entries + completion merge correctly
  - per-line fallback: chunk keeps failing -> single-line attempts
  - vad_options_instance: VadOptions (not dict) carries the 8s speech cap
  - contiguity_post_pass: next.start = max(next.start, prev.end)
  - write_srt_marker_cue: AI marker is a real timestamped first cue
  - sensevoice_min_dur_postpass: no <1s cues (extend / merge / drop ladder)
  - parse_exclusions: exclusions.jsonl ids parsed, garbage ignored
  - actions_consume: skip/retry/delete semantics on temp state/actions files
  - state_compaction: >2000 lines -> latest-per-key + 14d cutoff, atomic
  - jellyfin_refresh: noop without key; item match + refresh POST with key
  - refine_regenerate_uses_asr_cues: regenerate_asr -> asr_cues + cache cue list
  - refine_cache_hit_uses_cues_directly: cache cues used as dicts, no parse_srt
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orchestrator as o
import refine_subs


def build_cfg():
    return {
        "TRANSLATE_BASE": "http://127.0.0.1:8011/v1",
        "TRANSLATE_MODEL": "HY-MT1.5-7B-Q4_K_M.gguf",
        "SDH_PLACEHOLDERS": list(o.DEFAULT_SDH_PLACEHOLDERS),
    }


def fake_ja_lines(n):
    return [f"テストの台詞です。{i} はい、そうです。" for i in range(n)]


def fake_id_lines(n):
    return [f"Ini kalimat uji nomor {i}." for i in range(n)]


def test_merge_10_to_7():
    """Model returns 7 numbered entries for 10 lines; tail completion must fill
    the remaining 3 via a separate call, producing 10 groups."""
    calls = []

    def fake_post_chat(cfg, messages, model, key, local=False):
        sys_prompt = messages[0]["content"]
        n_lines = len([l for l in sys_prompt.splitlines() if re.match(r"^\d+\. ", l)]) or \
            len([l for l in messages[1]["content"].splitlines() if re.match(r"^\d+\. ", l)])
        calls.append(n_lines)
        if n_lines == 10:
            return "1. Satu.\n2. Dua.\n3. Tiga.\n4. Empat.\n5. Lima.\n6. Enam.\n7. Tujuh."
        if n_lines == 3:
            return "1. Delapan.\n2. Sembilan.\n3. Sepuluh."
        return "1. Per baris."

    o.post_chat = fake_post_chat
    lines = fake_ja_lines(10)
    groups = o._translate_merge_aware(build_cfg(), lines, "Indonesian", "k", refs=None)
    assert len(groups) == 10, f"expected 10 groups, got {len(groups)}"
    assert groups[0] == (0, 1, "Satu.")
    assert groups[9] == (9, 10, "Sepuluh."), groups[9]
    assert 10 in calls and 3 in calls, calls
    print("PASS merge_10_to_7 (calls:", calls, ")")


def test_echo_then_recover():
    """First chunk attempt echoes CJK; next attempt returns clean ID."""
    attempts = {"n": 0}

    def fake_post_chat(cfg, messages, model, key, local=False):
        n_lines = len([l for l in messages[1]["content"].splitlines() if re.match(r"^\d+\. ", l)])
        attempts["n"] += 1
        if n_lines == 10 and attempts["n"] <= 2:
            return "1. テストの台詞です。1\n2. はい、そうです。2\n3. 台詞3\n4. 台詞4\n5. 台詞5\n6. 台詞6\n7. 台詞7\n8. 台詞8\n9. 台詞9\n10. 台詞10"
        if n_lines == 10:
            return "1. Kalimat satu.\n2. Kalimat dua.\n3. Kalimat tiga.\n4. Kalimat empat.\n5. Kalimat lima.\n6. Kalimat enam.\n7. Kalimat tujuh.\n8. Kalimat delapan.\n9. Kalimat sembilan.\n10. Kalimat sepuluh."
        return "1. Per baris."

    o.post_chat = fake_post_chat
    groups = o._translate_merge_aware(build_cfg(), fake_ja_lines(10), "Indonesian", "k")
    assert len(groups) == 10
    assert groups[0][2] == "Kalimat satu.", groups[0]
    assert attempts["n"] >= 3, f"expected retries, got {attempts['n']} attempts"
    print(f"PASS echo_then_recover (attempts: {attempts['n']})")


def test_tail_completion_only():
    """Entries + completion merge: 7 entries + 3-line tail; completion may
    return fewer than asked (model merges), groups still aligned per entry."""
    def fake_post_chat(cfg, messages, model, key, local=False):
        n_lines = len([l for l in messages[1]["content"].splitlines() if re.match(r"^\d+\. ", l)])
        if n_lines == 10:
            return "1. A.\n2. B.\n3. C.\n4. D.\n5. E.\n6. F.\n7. G."
        if n_lines == 3:
            return "1. H.\n2. I.\n3. J."
        return "1. X."

    o.post_chat = fake_post_chat
    groups = o._translate_merge_aware(build_cfg(), fake_ja_lines(10), "Indonesian", "k")
    assert len(groups) == 10, groups
    assert [g[2] for g in groups] == [f"{c}." for c in "ABCDEFGHIJ"], groups
    print("PASS tail_completion_only")


def test_per_line_fallback():
    """Chunk always echoes; per-line fallback runs and emits empty text on
    persistent failure (mirrors one_shot 1-empty-cue tolerance)."""
    def fake_post_chat(cfg, messages, model, key, local=False):
        n_lines = len([l for l in messages[1]["content"].splitlines() if re.match(r"^\d+\. ", l)])
        if n_lines >= 2:
            return "1. エコー\n2. エコー\n3. エコー\n4. エコー\n5. エコー"
        return "1. エコー"

    o.post_chat = fake_post_chat
    groups = o._translate_merge_aware(build_cfg(), fake_ja_lines(5), "Indonesian", "k")
    assert len(groups) == 5, groups
    assert all(g[1] - g[0] == 1 for g in groups)
    assert all(g[2] == "" for g in groups), groups
    print("PASS per_line_fallback (5 empty)")


def test_glossary_refs_flow():
    """terminology block flows into the system prompt of every chunk/tail/fallback call."""
    seen = []

    def fake_post_chat(cfg, messages, model, key, local=False):
        seen.append(messages[0]["content"])
        n_lines = len([l for l in messages[1]["content"].splitlines() if re.match(r"^\d+\. ", l)])
        if n_lines == 10:
            return "1. Satu.\n2. Dua.\n3. Tiga.\n4. Empat.\n5. Lima.\n6. Enam.\n7. Tujuh."
        if n_lines == 3:
            return "1. Delapan.\n2. Sembilan.\n3. Sepuluh."
        return "1. Satu."

    o.post_chat = fake_post_chat
    refs = "アーシア翻译成Asia；イッセー翻译成Issei"
    groups = o._translate_merge_aware(build_cfg(), fake_ja_lines(10), "Indonesian", "k", refs=refs)
    assert len(groups) == 10
    for s in seen:
        assert "参考下面的翻译：\n" + refs in s, s[:80]
    print("PASS glossary_refs_flow")


def test_sanitize_guard_contract():
    """guard_foreign_lines with SDH placeholders keeps current behavior by default."""
    lines = ["これは日本語です。", "Some English text here", "中文文本", "また日本語"]
    guarded, foreign = o.guard_foreign_lines(lines)
    assert guarded[0] == lines[0]
    assert guarded[1] == "（歌詞）", guarded[1]
    assert guarded[2] == "（歌詞）", guarded[2]
    assert guarded[3] == lines[3]
    assert len(foreign) == 2
    cfg = {"sdh_placeholders": '["（歌詞）", "（効果音）"]'}
    guarded2, _ = o.guard_foreign_lines(lines, eval(cfg["sdh_placeholders"]))
    assert guarded2[1] == "（効果音）" and guarded2[2] == "（歌詞）", guarded2
    print("PASS sanitize_guard_contract")


def test_load_config_sdh():
    os.environ["STATE_FILE"] = "/tmp/dry_state.jsonl"
    with open("/tmp/dry_env.txt", "w") as fh:
        fh.write("TARGET_LANGS=id,en\nMAX_EPS_PER_RUN=2\nTRANSLATE_MODEL=x\n")
    o.ENV_FILE = "/tmp/dry_env.txt"
    o.OVERRIDE_FILE = "/tmp/dry_ovr.json"
    with open("/tmp/dry_ovr.json", "w") as fh:
        fh.write('{"sdh_placeholders": ["（歌詞）", "（効果音）"]}')
    cfg = o.load_config()
    assert cfg["SDH_PLACEHOLDERS"] == ["（歌詞）", "（効果音）"], cfg["SDH_PLACEHOLDERS"]
    with open("/tmp/dry_ovr.json", "w") as fh:
        fh.write("{}")
    cfg = o.load_config()
    assert cfg["SDH_PLACEHOLDERS"] == ["（歌詞）"], cfg["SDH_PLACEHOLDERS"]
    print("PASS load_config_sdh")




def _fake_word(start, end, word):
    class W:
        pass
    w = W()
    w.start, w.end, w.word = start, end, word
    return w


def test_segment_splitter():
    import pipeline.asr as pasr

    # continuous speech 20s, words 1s apart with 100ms gaps -> must split at 8s caps
    words = [_fake_word(i + 0.0, i + 0.9, f" word{i}") for i in range(20)]
    cues = pasr._split_segment(words, 0, 20000)
    assert len(cues) == 3, cues  # 0-8, 8-16, 16-20
    assert all(c["end"] - c["start"] <= 8000 for c in cues), cues
    assert cues[0]["start"] == 0 and cues[-1]["end"] == 19900
    assert cues[0]["text"] == "word0 word1 word2 word3 word4 word5 word6 word7", cues[0]["text"]

    # hard silence 2s inside -> split there even below cap
    words2 = [_fake_word(0.0, 1.0, " a"), _fake_word(3.0, 4.0, " b")]
    cues2 = pasr._split_segment(words2, 0, 4000)
    assert len(cues2) == 2 and cues2[0]["text"] == "a" and cues2[1]["text"] == "b", cues2

    # single 9s word (scream) -> kept whole
    words3 = [_fake_word(0.0, 9.0, "あああああ")]
    cues3 = pasr._split_segment(words3, 0, 9000)
    assert len(cues3) == 1 and cues3[0]["end"] - cues3[0]["start"] == 9000, cues3

    # gap >= SPLIT_MIN_SILENCE_MS (500ms) preferred as cut when over cap
    words4 = [
        _fake_word(0.0, 1.0, " a"),
        _fake_word(1.05, 2.05, " b"),
        _fake_word(2.6, 3.6, " c"),   # 550ms gap after b
        _fake_word(3.65, 4.65, " d"),
        _fake_word(4.7, 5.7, " e"),
        _fake_word(5.75, 6.75, " f"),
        _fake_word(6.8, 7.8, " g"),
        _fake_word(7.85, 8.85, " h"),
        _fake_word(8.9, 9.9, " i"),
    ]
    cues4 = pasr._split_segment(words4, 0, 9900)
    assert all(c["end"] - c["start"] <= 8000 for c in cues4), cues4
    assert len(cues4) == 2, cues4
    print("PASS segment_splitter", [f"{c['start']}-{c['end']}:{c['text'][:20]}" for c in cues])


def test_vad_options_instance():
    """VAD must be a VadOptions INSTANCE (dict silently overrides the 8s cap
    with chunk_length); fallback path yields None (dict + chunk_length=8)."""
    import pipeline.asr as pasr

    vp = pasr.vad_parameters()
    if pasr._VAD_OPTIONS_CLS is None:
        assert vp is None
        print("PASS vad_options_instance (fallback: VadOptions unavailable)")
        return
    assert vp is not None and type(vp).__name__ == "VadOptions", type(vp)
    assert float(vp.threshold) == 0.5
    assert int(vp.min_silence_duration_ms) == 200
    assert float(vp.max_speech_duration_s) == 8.0
    if hasattr(vp, "min_silence_at_max_speech"):
        assert float(vp.min_silence_at_max_speech) == 98.0
    if hasattr(vp, "speech_pad_ms"):
        assert int(vp.speech_pad_ms) == 250
    saved = pasr._VAD_OPTIONS_CLS
    pasr._VAD_OPTIONS_CLS = None
    try:
        assert pasr.vad_parameters() is None
    finally:
        pasr._VAD_OPTIONS_CLS = saved
    print("PASS vad_options_instance")


def test_contiguity_post_pass():
    """ensure_contiguous: sorted, next.start = max(next.start, prev.end)."""
    import pipeline.asr as pasr

    cues = [
        {"start": 5000, "end": 9000, "text": "b"},
        {"start": 0, "end": 4000, "text": "a"},
        {"start": 3500, "end": 7000, "text": "c"},
    ]
    out = pasr.ensure_contiguous(cues)
    assert [c["start"] for c in out] == [0, 4000, 7000], out
    for i in range(1, len(out)):
        assert out[i]["start"] >= out[i - 1]["end"], out
    assert out is cues, "must be the same list object, sorted in place"
    print("PASS contiguity_post_pass")


def test_sensevoice_min_dur_postpass():
    """(a) extend to 1s when gap allows; (b) merge forward on tight gap;
    (c) two 0.3s cues -> merged then extended >=1s; (d) 5.9s+0.4s pair:
    cap 6s respected, short one extended; (e) closer neighbor preferred;
    (f) tie -> previous; (g) unresolvable fragment dropped. Invariant: no
    cue < 1s ever emitted."""
    import pipeline.sensevoice as psv

    def c(s, e, t):
        return {"start": s, "end": e, "text": t}

    def check(label, out):
        assert all(o["end"] - o["start"] >= 1000 for o in out), (label, out)
        return out

    # (a) 0.4s cue, 2.0s gap after -> extended to 1.0s, gap >= 150ms kept
    out = check("a", psv._min_dur_postpass([c(0, 400, "a"), c(2400, 3600, "b")]))
    assert out[0] == {"start": 0, "end": 1000, "text": "a"}, out
    assert out[1]["start"] - out[0]["end"] >= 150

    # (b) 0.4s cue, 0.3s gap to next -> merged forward
    out = check("b", psv._min_dur_postpass([c(0, 400, "a"), c(700, 2700, "bb")]))
    assert len(out) == 1 and out[0]["text"] == "a bb" and out[0]["end"] == 2700, out

    # (c) two 0.3s cues, 0.1s gap -> merged pair (0.7s) then extended to 1.0s
    out = check("c", psv._min_dur_postpass([c(0, 300, "x"), c(400, 700, "y")]))
    assert len(out) == 1 and out[0]["text"] == "x y" and out[0]["end"] == 1000, out

    # (d) 5.9s + 0.4s pair -> NO merge over 6s cap; short cue extended instead
    out = check("d", psv._min_dur_postpass([c(0, 5900, "long"), c(6200, 6600, "z")]))
    assert len(out) == 2, out
    assert out[0]["end"] - out[0]["start"] == 5900 and out[0]["text"] == "long", out
    assert out[1]["start"] == 6200 and out[1]["end"] == 7200, out

    # (e) closer neighbor preferred (prev gap 200 vs next gap 300; extension
    #     impossible since next is only 300ms away) -> previous absorbs tiny
    out = check("e", psv._min_dur_postpass([c(0, 2000, "p"), c(2200, 2600, "tiny"), c(2900, 4300, "n")]))
    assert out[0]["text"] == "p tiny" and out[0]["end"] == 2600, out
    assert len(out) == 2

    # (f) equal gaps -> previous wins
    out = check("f", psv._min_dur_postpass([c(0, 2000, "p"), c(2150, 2450, "tiny"), c(2600, 4000, "n")]))
    assert out[0]["text"] == "p tiny", out

    # (g) 0.2s fragment between 5.9s neighbors, gaps 200/50 -> no merge fits
    #     (previous span 6300 > 6000, next span 6250 > 6000), clamp extend
    #     impossible -> dropped; no <1s cue in output
    out = check("g", psv._min_dur_postpass([c(0, 5900, "long"), c(6100, 6300, "tiny"), c(6350, 12350, "next")]))
    assert len(out) == 2 and all("tiny" != o["text"] for o in out), out

    # (h) trailing short cue extends freely even with a 5.9s neighbor before
    out = check("h", psv._min_dur_postpass([c(0, 5900, "long"), c(6100, 6500, "tail")]))
    assert out[1]["end"] - out[1]["start"] == 1000 and out[1]["text"] == "tail", out
    print("PASS sensevoice_min_dur_postpass")


def test_write_srt_marker_cue():
    """AI marker is a REAL first cue (timestamped), never a bare header line;
    marker end bumps to just before the first dialogue cue when it is < 2s."""
    cues = [
        {"start": 5000, "end": 9000, "text": "Dia menoleh."},
        {"start": 9000, "end": 13000, "text": "Lalu tertawa."},
    ]
    texts = [c["text"] for c in cues]
    out = "/tmp/dry_marker1.srt"
    o.write_srt(cues, texts, out, header=o.AI_MARKER)
    raw = open(out, encoding="utf-8").read()
    first = raw.splitlines()[0]
    assert first != o.AI_MARKER, "bare marker header line must not exist"
    assert first == "1", first
    assert raw.startswith(
        f"1\n00:00:00,000 --> 00:00:01,500\n{o.AI_MARKER}\n\n2\n"
    ), raw[:160]
    assert "00:00:05,000 --> 00:00:09,000" in raw

    early = [{"start": 500, "end": 4000, "text": "X"}]
    out2 = "/tmp/dry_marker2.srt"
    o.write_srt(early, ["X"], out2, header=o.AI_MARKER)
    raw2 = open(out2, encoding="utf-8").read()
    assert "00:00:00,000 --> 00:00:00,499\n" in raw2, raw2[:160]
    assert "2\n00:00:00,500 -->" in raw2, raw2

    saved = o.AI_MARKER_CUE
    o.AI_MARKER_CUE = False
    try:
        out3 = "/tmp/dry_marker3.srt"
        o.write_srt(cues, texts, out3, header=o.AI_MARKER)
    finally:
        o.AI_MARKER_CUE = saved
    raw3 = open(out3, encoding="utf-8").read()
    assert raw3.startswith(o.AI_MARKER + "\n\n1\n"), raw3[:60]
    print("PASS write_srt_marker_cue")


def test_parse_exclusions():
    """exclusions.jsonl ids are parsed, garbage/bad types ignored."""
    import tempfile

    path = "/tmp/dry_exclusions.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"episode_id": 5, "reason": "dup"}\n')
        fh.write('{"episode_id": "7"}\n')
        fh.write("not-json\n")
        fh.write('{"episode_id": 7}\n')
    o.EXCLUSIONS_FILE = path
    ids = o.parse_exclusions()
    o.EXCLUSIONS_FILE = "/home/user/.config/asr-pipeline/exclusions.jsonl"
    assert ids == {5, 7}, ids
    print("PASS parse_exclusions")


def test_actions_consume():
    """skip -> returned for this pass; retry -> state cleared AND subtitle
    files removed (Bazarr wanted only lists episodes with MISSING subs, so
    state-only clearing would make Retry dead); delete -> SRT files removed
    (NAS + TMP), state cleared, jellyfin refresh fired; actions.jsonl
    truncated atomically."""
    import tempfile

    d = tempfile.mkdtemp()
    state_file = os.path.join(d, "state.jsonl")
    actions_file = os.path.join(d, "actions.jsonl")
    with open(state_file, "w", encoding="utf-8") as fh:
        for eid, lang in ((10, "id"), (10, "en"), (20, "id"), (30, "id")):
            fh.write(
                json.dumps(
                    {"sonarrEpisodeId": eid, "language": lang, "status": "done"}
                )
                + "\n"
            )
    with open(actions_file, "w", encoding="utf-8") as fh:
        fh.write('{"type": "skip", "episode_id": 10, "ts": "t"}\n')
        fh.write('{"type": "retry", "episode_id": 20, "ts": "t"}\n')
        fh.write('{"type": "retry", "episode_id": 40, "language": "en", "ts": "t"}\n')
        fh.write('{"action": "delete", "episode_id": 30, "ts": "t"}\n')

    ep_dir = os.path.join(d, "ep")
    os.makedirs(ep_dir)
    mkv = os.path.join(ep_dir, "Ep.mkv")
    keep_mkv = os.path.join(ep_dir, "Ep.other.srt")
    for p in (mkv, keep_mkv, os.path.join(ep_dir, "Ep.id.srt"), os.path.join(ep_dir, "Ep.en.srt")):
        open(p, "w").close()
    tmp_dir = os.path.join(d, "tmp")
    os.makedirs(tmp_dir)
    for p in (os.path.join(tmp_dir, "30_id.srt"), os.path.join(tmp_dir, "20_id.srt")):
        open(p, "w").close()

    saved_state, saved_actions = o.STATE_FILE, o.ACTIONS_FILE
    saved_get_episode, saved_jf = o.get_episode, o.jellyfin_refresh
    o.STATE_FILE, o.ACTIONS_FILE = state_file, actions_file
    o.get_episode = lambda cfg, eid: {
        "title": "Ep Title",
        "episodeFile": {"path": mkv},
    }
    refreshes = []
    o.jellyfin_refresh = lambda cfg, media_path, title=None: refreshes.append(
        (media_path, title)
    )
    try:
        cfg = build_cfg()
        cfg["TARGET_LANGS"] = ["id", "en"]
        cfg["TMP_DIR"] = tmp_dir
        skip = o.consume_actions(cfg)
    finally:
        o.STATE_FILE, o.ACTIONS_FILE = saved_state, saved_actions
        o.get_episode, o.jellyfin_refresh = saved_get_episode, saved_jf

    assert skip == {10}, skip
    remaining = o.load_records_jsonl(state_file)
    keys = {(r["sonarrEpisodeId"], r["language"]) for r in remaining}
    assert (10, "id") in keys and (10, "en") in keys
    assert (20, "id") not in keys, "retry must clear all state for episode 20"
    assert (30, "id") not in keys, "delete must clear done state"
    assert o.load_records_jsonl(actions_file) == [], "actions file must be empty"
    assert os.path.isfile(mkv) and os.path.isfile(keep_mkv)
    assert not os.path.exists(os.path.join(ep_dir, "Ep.id.srt")), "NAS id srt removed"
    assert not os.path.exists(os.path.join(ep_dir, "Ep.en.srt")), "NAS en srt removed"
    assert not os.path.exists(os.path.join(tmp_dir, "30_id.srt")), "TMP 30 srt removed"
    assert not os.path.exists(os.path.join(tmp_dir, "20_id.srt")), (
        "retry must remove TMP srt too (Bazarr wanted only lists missing subs)"
    )
    assert len(refreshes) == 1 and refreshes[0][0].endswith("Ep.mkv"), refreshes
    assert refreshes[0][1] == "Ep Title", refreshes
    print("PASS actions_consume")


def test_state_compaction():
    """>2000 state entries -> keep only the LATEST per (episode, language),
    drop >14d-old entries, rewrite atomically; schema unchanged."""
    import tempfile

    d = tempfile.mkdtemp()
    state_file = os.path.join(d, "state.jsonl")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (
        datetime.now(timezone.utc) - timedelta(days=20)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(state_file, "w", encoding="utf-8") as fh:
        for i in range(1, 2001):
            fh.write(
                json.dumps(
                    {"sonarrEpisodeId": i, "language": "id", "status": "done", "ts": now}
                )
                + "\n"
            )
        fh.write(
            json.dumps(
                {"sonarrEpisodeId": 999, "language": "id", "status": "done", "ts": old}
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {"sonarrEpisodeId": 5, "language": "id", "status": "done", "ts": now}
            )
            + "\n"
        )
    saved = o.STATE_FILE
    o.STATE_FILE = state_file
    try:
        entries = o.load_state()
    finally:
        o.STATE_FILE = saved
    keys = {(e["sonarrEpisodeId"], e["language"]) for e in entries}
    assert (999, "id") not in keys, "old entry (>14d) must be dropped"
    assert len(entries) == 1999, len(entries)
    # atomically rewritten: file on disk is compacted, entry per key is latest
    on_disk = o.load_records_jsonl(state_file)
    assert len(on_disk) == len(entries) == 1999, (len(on_disk), len(entries))
    assert [e["sonarrEpisodeId"] for e in on_disk].count(5) == 1
    print("PASS state_compaction")


def test_jellyfin_refresh():
    """No API key -> complete no-op (no network). With key: item located by
    mapped path and refresh POST fired with MetadataRefreshMode."""
    class FakeResp:
        def __init__(self, obj, status=200):
            self._obj, self.status_code = obj, status

        def json(self):
            return self._obj

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"HTTP {self.status_code}")

    calls = []

    class FakeRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            calls.append(("GET", url, dict(params or {})))
            return FakeResp(
                {
                    "Items": [
                        {"Id": "abc123", "Path": "/media/jellyfin/sonarr-tv-shows/X/Ep.mkv"}
                    ]
                }
            )

        @staticmethod
        def post(url, json=None, headers=None, timeout=None):
            calls.append(("POST", url, dict(json or {})))
            return FakeResp(None, 204)

    saved_requests = o.requests
    o.requests = FakeRequests
    try:
        cfg = {"JELLYFIN_API_KEY": "", "JELLYFIN_URL": "http://jf:8096"}
        o.jellyfin_refresh(cfg, "/mnt/nas/share/media/jellyfin/sonarr-tv-shows/X/Ep.mkv", "Ep Title")
        assert calls == [], calls

        cfg2 = dict(cfg, JELLYFIN_API_KEY="k123")
        o.jellyfin_refresh(cfg2, "/mnt/nas/share/media/jellyfin/sonarr-tv-shows/X/Ep.mkv", "Ep Title")
        deadline = time.time() + 5
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert len(calls) == 2, calls
        m1 = calls[0]
        assert m1[0] == "GET" and m1[1].endswith("/Items"), m1
        assert m1[2]["SearchTerm"] == "Ep Title" and m1[2]["IncludeItemTypes"] == "Episode"
        m2 = calls[1]
        assert m2[0] == "POST" and m2[1].endswith("/Items/abc123/Refresh"), m2
        assert m2[2]["MetadataRefreshMode"] == "FullRefresh", m2
    finally:
        o.requests = saved_requests
    print("PASS jellyfin_refresh (noop without key; item match + refresh POST)")



def test_translate_overcount_clamped():
    """Model returns MORE numbered lines than the chunk (12 for 10):
    entries with k > n must be dropped so exactly n groups are produced and
    cue assembly (cues[s]) never overruns the chunk."""

    def fake_post_chat(cfg, messages, model, key, local=False):
        return "\n".join(f"{i}. Baris nomor {i}." for i in range(1, 13))

    o.post_chat = fake_post_chat
    groups = o._translate_merge_aware(
        build_cfg(), fake_ja_lines(10), "Indonesian", "k"
    )
    assert len(groups) == 10, f"over-count dropped: {len(groups)} groups"
    assert [g[0] for g in groups] == list(range(10)), groups
    cues = [
        {"start": i * 1000, "end": i * 1000 + 900, "text": f"cue {i}"}
        for i in range(10)
    ]
    for s, e, _t in groups:
        assert 0 <= s < len(cues) and s < e <= len(cues), (s, e)
    print("PASS translate_overcount_clamped")


def test_delete_lang_scoped():
    """_delete_episode_subtitles(cfg, eid, langs=["id"]) removes only the id
    SRTs (NAS + TMP); en files and the video stay untouched."""
    import tempfile

    d = tempfile.mkdtemp()
    ep_dir = os.path.join(d, "ep")
    os.makedirs(ep_dir)
    mkv = os.path.join(ep_dir, "Ep.mkv")
    for p in (
        mkv,
        os.path.join(ep_dir, "Ep.id.srt"),
        os.path.join(ep_dir, "Ep.en.srt"),
    ):
        open(p, "w").close()
    tmp_dir = os.path.join(d, "tmp")
    os.makedirs(tmp_dir)
    for p in (os.path.join(tmp_dir, "77_id.srt"), os.path.join(tmp_dir, "77_en.srt")):
        open(p, "w").close()

    saved_get_episode = o.get_episode
    o.get_episode = lambda cfg, eid: {"episodeFile": {"path": mkv}}
    try:
        cfg = build_cfg()
        cfg["TARGET_LANGS"] = ["id", "en"]
        cfg["TMP_DIR"] = tmp_dir
        deleted = o._delete_episode_subtitles(cfg, 77, langs=["id"])
    finally:
        o.get_episode = saved_get_episode

    id_srt, en_srt = os.path.join(ep_dir, "Ep.id.srt"), os.path.join(ep_dir, "Ep.en.srt")
    t_id, t_en = os.path.join(tmp_dir, "77_id.srt"), os.path.join(tmp_dir, "77_en.srt")
    assert not os.path.exists(id_srt), "id srt must be removed"
    assert os.path.isfile(en_srt), "en srt must stay"
    assert not os.path.exists(t_id), "tmp id srt must be removed"
    assert os.path.isfile(t_en), "tmp en srt must stay"
    assert os.path.isfile(mkv), "video must stay"
    assert sorted(deleted) == sorted([id_srt, t_id]), deleted
    print("PASS delete_lang_scoped")



def test_refine_regenerate_uses_asr_cues():
    """regenerate_asr must use the v2 asr_cues() API (cue dicts; asr_srt was
    deleted in rewrite) and cache the cues LIST via asr_cache_put."""
    import tempfile

    d = tempfile.mkdtemp()
    wav = os.path.join(d, "in.wav")
    open(wav, "w").close()

    fake_cues = [
        {"start_ms": 0, "end_ms": 1000, "text": "hai."},
        {"start_ms": 1000, "end_ms": 2500, "text": "sou desu."},
    ]
    saved_probe, saved_choose, saved_extract = o.probe_audio, o.choose_source, o.extract_wav
    saved_asr_cues, saved_cache_put = o.asr_cues, o.asr_cache_put
    o.probe_audio = lambda video_path: [{"index": 1, "language": "jpn", "codec": "aac"}]
    o.choose_source = lambda streams, want: {"stream_index": 1, "asr_lang": "ja"}
    o.extract_wav = lambda video_path, idx, out: None
    o.asr_cues = lambda cfg, wav_path, lang: fake_cues
    cached = []
    o.asr_cache_put = lambda ep_id, asr_lang, media_path, cues: cached.append(
        (ep_id, asr_lang, media_path, cues)
    )
    try:
        cfg = build_cfg()
        cfg["TMP_DIR"] = d
        got = refine_subs.regenerate_asr(cfg, 42, "x.mkv")
    finally:
        o.probe_audio, o.choose_source, o.extract_wav = saved_probe, saved_choose, saved_extract
        o.asr_cues, o.asr_cache_put = saved_asr_cues, saved_cache_put
    assert got == (fake_cues, True), got
    assert cached == [(42, "ja", "x.mkv", fake_cues)], cached
    print("PASS refine_regenerate_uses_asr_cues")


def test_refine_cache_hit_uses_cues_directly():
    """cache hit returns the cue dict LIST untouched; the refine flow must
    NOT call parse_srt on it (parse_srt expects SRT text with --> timestamps
    and would AttributeError on a list)."""
    fake_cues = [
        {"start_ms": 0, "end_ms": 1000, "text": "hai."},
        {"start_ms": 1000, "end_ms": 2500, "text": "sou desu."},
    ]
    saved_cache_get, saved_parse = o.asr_cache_get, o.parse_srt
    o.asr_cache_get = lambda ep_id, asr_lang, media_path: fake_cues
    parse_calls = []
    o.parse_srt = lambda text: parse_calls.append(text) or [{"text": "WRONG"}]
    try:
        cues, regen = refine_subs.get_asr_text(build_cfg(), 42, "x.mkv", no_regen=True)
    finally:
        o.asr_cache_get, o.parse_srt = saved_cache_get, saved_parse
    assert cues == fake_cues, cues
    assert regen is False
    assert parse_calls == [], "parse_srt must not be called on cache cues"
    assert [c["text"] for c in cues] == ["hai.", "sou desu."]
    print("PASS refine_cache_hit_uses_cues_directly")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL DRY TESTS PASSED")
