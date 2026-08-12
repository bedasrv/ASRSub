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
  - actions_truncate_preserves_appended: tail-preserving consume keeps concurrent appends
  - translate_offset_keys_clamped: m==n offset-numbered keys (2..n+1) clamped
  - delete_lang_scoped_action: language-scoped delete + null language = all
  - target_langs_json_list_override: TARGET_LANGS list override parses
  - state_counts_latest_per_key: counts are latest-per-(episode, language)
  - gc_state_orphans: ghost/fileless pruning, 24h age guard, session memoization
  - validate_retry_target: retry/delete reject ghost/fileless/unmonitored
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orchestrator as o
import control_api_v2 as api2
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



def test_actions_truncate_preserves_appended():
    """tail-preserving consumption: the consumed prefix is removed in place,
    records appended by a concurrent writer survive the consume and are
    processed on the next pass."""
    import tempfile

    d = tempfile.mkdtemp()
    state_file = os.path.join(d, "state.jsonl")
    actions_file = os.path.join(d, "actions.jsonl")
    with open(state_file, "w", encoding="utf-8") as fh:
        for eid, lang in ((10, "id"), (10, "en"), (20, "id")):
            fh.write(
                json.dumps({"sonarrEpisodeId": eid, "language": lang, "status": "done"})
                + "\n"
            )
    with open(actions_file, "w", encoding="utf-8") as fh:
        fh.write('{"type": "retry", "episode_id": 10, "ts": "t"}\n')
        fh.write('{"type": "skip", "episode_id": 20, "ts": "t"}\n')

    recs = o.consume_records_jsonl(actions_file)
    assert len(recs) == 2, recs
    assert recs[0]["episode_id"] == 10 and recs[1]["episode_id"] == 20, recs
    with open(actions_file, "r", encoding="utf-8") as fh:
        assert fh.read() == "", "consumed prefix must be removed in place"

    with open(actions_file, "a", encoding="utf-8") as fh:
        fh.write('{"type": "retry", "episode_id": 999, "ts": "t2"}\n')

    saved_state, saved_actions = o.STATE_FILE, o.ACTIONS_FILE
    saved_del, saved_jf = o._delete_episode_subtitles, o.jellyfin_refresh
    o.STATE_FILE, o.ACTIONS_FILE = state_file, actions_file
    dels = []
    o._delete_episode_subtitles = lambda cfg, eid, langs=None: dels.append(eid) or []
    o.jellyfin_refresh = lambda cfg, media_path, title=None: None
    try:
        cfg = build_cfg()
        cfg["TARGET_LANGS"] = ["id", "en"]
        cfg["TMP_DIR"] = d
        skip = o.consume_actions(cfg)
        assert skip == set(), skip
        assert dels == [999], "appended record must be processed on next pass"
        with open(actions_file, "r", encoding="utf-8") as fh:
            assert fh.read() == "", "tail consumed on the next pass"
    finally:
        o.STATE_FILE, o.ACTIONS_FILE = saved_state, saved_actions
        o._delete_episode_subtitles, o.jellyfin_refresh = saved_del, saved_jf
    print("PASS actions_truncate_preserves_appended")



def test_translate_offset_keys_clamped():
    """Model returns exactly n numbered lines but offset-numbered (2..n+1):
    the m==n branch must drop out-of-range keys instead of producing a group
    beyond the cue list (IndexError) or negative indices (silent wrap)."""

    def fake_post_chat(cfg, messages, model, key, local=False):
        return "\n".join(f"{i}. Baris offset {i}." for i in range(2, 12))

    o.post_chat = fake_post_chat
    groups = o._translate_merge_aware(
        build_cfg(), fake_ja_lines(10), "Indonesian", "k"
    )
    assert len(groups) == 9, f"keys 2..10 kept, 11 dropped: {groups}"
    assert [g[0] for g in groups] == list(range(1, 10)), groups
    assert all(0 <= g[0] < 10 and g[1] <= 10 for g in groups), groups
    cues = [
        {"start": i * 1000, "end": i * 1000 + 900, "text": f"cue {i}"}
        for i in range(10)
    ]
    for s, e, _t in groups:
        assert 0 <= s < len(cues) and s < e <= len(cues), (s, e)
    print("PASS translate_offset_keys_clamped")


def test_delete_lang_scoped_action():
    """delete action with language removes only that language's SRT files and
    done-state; a retry with null language means the WHOLE episode."""
    import tempfile

    d = tempfile.mkdtemp()
    state_file = os.path.join(d, "state.jsonl")
    actions_file = os.path.join(d, "actions.jsonl")
    with open(state_file, "w", encoding="utf-8") as fh:
        for eid, lang in ((30, "id"), (30, "en"), (40, "id"), (40, "en")):
            fh.write(
                json.dumps({"sonarrEpisodeId": eid, "language": lang, "status": "done"})
                + "\n"
            )
    with open(actions_file, "w", encoding="utf-8") as fh:
        fh.write('{"action": "delete", "episode_id": 30, "language": "en", "ts": "t"}\n')
        fh.write('{"type": "retry", "episode_id": 40, "ts": "t"}\n')

    ep_dir = os.path.join(d, "ep")
    os.makedirs(ep_dir)
    mkv30 = os.path.join(ep_dir, "Ep30.mkv")
    mkv40 = os.path.join(ep_dir, "Ep40.mkv")
    for p in (
        mkv30,
        os.path.join(ep_dir, "Ep30.id.srt"),
        os.path.join(ep_dir, "Ep30.en.srt"),
        mkv40,
        os.path.join(ep_dir, "Ep40.id.srt"),
        os.path.join(ep_dir, "Ep40.en.srt"),
    ):
        open(p, "w").close()
    tmp_dir = os.path.join(d, "tmp")
    os.makedirs(tmp_dir)
    for p in (
        os.path.join(tmp_dir, "30_id.srt"),
        os.path.join(tmp_dir, "30_en.srt"),
        os.path.join(tmp_dir, "40_id.srt"),
        os.path.join(tmp_dir, "40_en.srt"),
    ):
        open(p, "w").close()

    saved_state, saved_actions = o.STATE_FILE, o.ACTIONS_FILE
    saved_get_episode, saved_jf = o.get_episode, o.jellyfin_refresh
    o.STATE_FILE, o.ACTIONS_FILE = state_file, actions_file
    o.get_episode = lambda cfg, eid: {
        "title": f"Ep Title {eid}",
        "episodeFile": {"path": os.path.join(ep_dir, f"Ep{eid}.mkv")},
    }
    refreshes = []
    o.jellyfin_refresh = lambda cfg, media_path, title=None: refreshes.append(
        (media_path, title)
    )
    try:
        cfg = build_cfg()
        cfg["TARGET_LANGS"] = ["id", "en"]
        cfg["TMP_DIR"] = tmp_dir
        o.consume_actions(cfg)
    finally:
        o.STATE_FILE, o.ACTIONS_FILE = saved_state, saved_actions
        o.get_episode, o.jellyfin_refresh = saved_get_episode, saved_jf

    assert not os.path.exists(os.path.join(ep_dir, "Ep30.en.srt")), "en srt removed"
    assert os.path.isfile(os.path.join(ep_dir, "Ep30.id.srt")), "id srt stays"
    assert not os.path.exists(os.path.join(tmp_dir, "30_en.srt")), "tmp en srt removed"
    assert os.path.isfile(os.path.join(tmp_dir, "30_id.srt")), "tmp id srt stays"
    assert not os.path.exists(os.path.join(ep_dir, "Ep40.id.srt")), "null retry all NAS"
    assert not os.path.exists(os.path.join(ep_dir, "Ep40.en.srt")), "null retry all NAS"
    assert not os.path.exists(os.path.join(tmp_dir, "40_id.srt")), "null retry all tmp"
    assert not os.path.exists(os.path.join(tmp_dir, "40_en.srt")), "null retry all tmp"
    keys = {
        (r["sonarrEpisodeId"], r["language"])
        for r in o.load_records_jsonl(state_file)
    }
    assert (30, "id") in keys, "id done state stays"
    assert (30, "en") not in keys, "en done state cleared"
    assert (40, "id") not in keys, "null retry clears all state"
    assert (40, "en") not in keys, "null retry clears all state"
    assert len(refreshes) == 1, refreshes
    print("PASS delete_lang_scoped_action")


def test_state_counts_latest_per_key():
    """_state_counts must count the LATEST row per (sonarrEpisodeId, language),
    not every row: old done + newer error for the same key counts the error
    once; same episode different language counts separately."""
    import tempfile

    d = tempfile.mkdtemp()
    state_file = os.path.join(d, "state.jsonl")
    t_old = "2026-08-01T00:00:00Z"
    t_new = "2026-08-02T00:00:00Z"
    with open(state_file, "w", encoding="utf-8") as fh:
        rows = [
            {"sonarrEpisodeId": 100, "language": "id", "status": "done", "ts": t_old},
            {"sonarrEpisodeId": 100, "language": "id", "status": "error", "ts": t_new},
            {"sonarrEpisodeId": 100, "language": "en", "status": "done", "ts": t_old},
            {"sonarrEpisodeId": 300, "language": "id", "status": "error", "ts": t_old},
            {"sonarrEpisodeId": 300, "language": "id", "status": "done", "ts": t_new},
            {"sonarrEpisodeId": 200, "language": "id", "ts": t_new},
        ]
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    saved = o.STATE_FILE
    o.STATE_FILE = state_file
    try:
        counts = o.ControlHandler.__new__(o.ControlHandler)._state_counts()
    finally:
        o.STATE_FILE = saved
    assert counts == {"done": 2, "error": 1, "pending": 1, "total": 4}, counts
    print("PASS state_counts_latest_per_key", counts)


def test_gc_state_orphans():
    """gc_state drops ghost ids (absent from Sonarr map) immediately, drops
    fileless rows only when older than 24h, keeps monitored-with-file rows
    (even old), unmonitored-with-file rows, recent fileless rows and rows with
    unparseable ts; rewrites state.jsonl atomically; never re-queries Sonarr
    for ids checked earlier in the session (mark_ghost 404s included)."""
    import tempfile

    d = tempfile.mkdtemp()
    state_file = os.path.join(d, "state.jsonl")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    # 501 ghost (not in Sonarr), 502 fileless old -> dropped; 503 fileless
    # recent -> kept; 504 normal old -> kept; 505 unmonitored-with-file ->
    # kept; 506 fileless unparseable ts -> kept; 507 ghost via get_episode 404
    rows = [
        {"sonarrEpisodeId": 501, "language": "id", "status": "error", "ts": now},
        {"sonarrEpisodeId": 502, "language": "id", "status": "error", "ts": old},
        {"sonarrEpisodeId": 503, "language": "id", "status": "error", "ts": now},
        {"sonarrEpisodeId": 504, "language": "id", "status": "done", "ts": old},
        {"sonarrEpisodeId": 505, "language": "id", "status": "done", "ts": old},
        {"sonarrEpisodeId": 506, "language": "id", "status": "error", "ts": "garbage-ts"},
        {"sonarrEpisodeId": 507, "language": "id", "status": "error", "ts": now},
    ]
    with open(state_file, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    canned = {
        502: {"monitored": True, "hasFile": False},
        503: {"monitored": True, "hasFile": False},
        504: {"monitored": True, "hasFile": True},
        505: {"monitored": False, "hasFile": True},
        506: {"monitored": True, "hasFile": False},
    }
    fetch_calls = []

    def fake_fetch(cfg):
        fetch_calls.append(1)
        return dict(canned)

    saved_state = o.STATE_FILE
    saved_fetch = o.fetch_sonarr_episode_map
    saved_mark = o.mark_ghost
    o.STATE_FILE = state_file
    o.fetch_sonarr_episode_map = fake_fetch
    try:
        o.mark_ghost(507)  # simulate a get_episode 404 during run_pass
        cfg = build_cfg()
        kept, stats = o.gc_state(cfg, o.load_state())
        assert stats == {"dropped": 3, "ghost": 2, "fileless": 1}, stats
        kept_ids = {e["sonarrEpisodeId"] for e in kept}
        assert kept_ids == {503, 504, 505, 506}, kept_ids
        assert len(fetch_calls) == 1
        # second sweep: ids already checked this session -> no Sonarr query,
        # known ghosts (from map or get_episode 404) still dropped
        with open(state_file, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"sonarrEpisodeId": 501, "language": "en", "status": "error", "ts": now}
                )
                + "\n"
            )
        kept2, stats2 = o.gc_state(cfg, o.load_state())
        assert len(fetch_calls) == 1, "must not re-query Sonarr for known ids"
        assert stats2 == {"dropped": 1, "ghost": 1, "fileless": 0}, stats2
        on_disk = o.load_records_jsonl(state_file)
        assert {e["sonarrEpisodeId"] for e in on_disk} == kept_ids, on_disk
    finally:
        o.STATE_FILE = saved_state
        o.fetch_sonarr_episode_map = saved_fetch
        o.mark_ghost = saved_mark
    print("PASS gc_state_orphans", stats)


def test_validate_retry_target():
    """retry/delete validation: rejects ghost (404), fileless (hasFile=false
    or no episodeFile.path), unmonitored, unreachable; allows healthy."""
    def fake_lookup(cfg, ep_id):
        by_id = {
            1: (404, None),
            2: (200, {"hasFile": False, "monitored": True}),
            3: (200, {"hasFile": True, "monitored": True, "episodeFile": {}}),
            4: (
                200,
                {"hasFile": True, "monitored": False, "episodeFile": {"path": "/x.mkv"}},
            ),
            5: (
                200,
                {"hasFile": True, "monitored": True, "episodeFile": {"path": "/x.mkv"}},
            ),
            6: (0, None),
        }
        return by_id.get(ep_id, (404, None))

    cfg = build_cfg()
    ok, err = api2.validate_retry_target(cfg, 1, lookup=fake_lookup)
    assert not ok and "deleted from Sonarr" in err, (ok, err)
    ok, err = api2.validate_retry_target(cfg, 2, lookup=fake_lookup)
    assert not ok and "no video file" in err, (ok, err)
    ok, err = api2.validate_retry_target(cfg, 3, lookup=fake_lookup)
    assert not ok and "no video file" in err, (ok, err)
    ok, err = api2.validate_retry_target(cfg, 4, lookup=fake_lookup)
    assert not ok and "unmonitored" in err, (ok, err)
    ok, err = api2.validate_retry_target(cfg, 5, lookup=fake_lookup)
    assert ok and err is None, (ok, err)
    ok, err = api2.validate_retry_target(cfg, 6, lookup=fake_lookup)
    assert not ok and "cannot verify" in err, (ok, err)
    print("PASS validate_retry_target")


def _write_srt(path, cues, ratio_kind="jpn"):
    """Write a minimal SRT whose cleaned text is mostly CJK (jpn) or latin
    (eng), with cues spread across the given span."""
    with open(path, "w", encoding="utf-8") as fh:
        for i, (start_ms, end_ms, text) in enumerate(cues, 1):
            fh.write(f"{i}\n{o._fmt_ms(start_ms)} --> {o._fmt_ms(end_ms)}\n{text}\n\n")
    return path


def test_ladder_registry():
    """registry upsert/get/load: rows keyed (stem, lang) — the on-disk sidecar
    is the unit of provenance — latest row wins, created_ts kept,
    source/source_path/source_hash/audio_id round-tripped, ep_id nullable
    (webhook rows), legacy episode_id-only rows still load and resolve."""
    import tempfile

    d = tempfile.mkdtemp()
    saved = o.REGISTRY_FILE
    o.REGISTRY_FILE = os.path.join(d, "subtitle_registry.jsonl")
    try:
        e1 = o.registry_upsert("/tv/S1/Ep1", "id", "asr", ep_id=5)
        time.sleep(0.01)
        e2 = o.registry_upsert("/tv/S1/Ep1", "id", "jpn", source_path="/x.jpn.srt", source_hash="abc")
        e3 = o.registry_upsert("/tv/S1/Ep2", "en", "eng")
        e4 = o.registry_upsert("/tv/S1/Ep1", "jpn", "embedded",
                               source_kind="embedded", audio_id="a1b2")
        assert e1["created_ts"] == e2["created_ts"], (e1, e2)
        assert e2["updated_ts"] >= e1["updated_ts"]
        assert e2["source"] == "jpn" and e2["source_hash"] == "abc"
        assert e4["episode_id"] is None and e4["audio_id"] == "a1b2", e4
        assert e4["source_kind"] == "embedded"
        reg = o.load_registry()
        assert set(reg) == {("/tv/S1/Ep1", "id"), ("/tv/S1/Ep2", "en"),
                            ("/tv/S1/Ep1", "jpn")}, set(reg)
        assert reg[("/tv/S1/Ep1", "id")]["source"] == "jpn", "latest row must win"
        assert o.registry_get("/tv/S1/Ep1", "id")["source"] == "jpn"
        assert o.registry_get("/tv/S1/Ep1", "jpn")["source_kind"] == "embedded"
        assert o.registry_get(None, "id", media_path="/tv/S1/Ep1.mkv")["source"] == "jpn"
        assert o.registry_get("/x", "id") is None
        # legacy row (episode_id only, no stem) still loads + resolves by ep_id
        with open(o.REGISTRY_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"episode_id": 99, "lang": "id", "source": "asr",
                                 "source_path": "", "source_hash": "",
                                 "created_ts": "2026-08-01T00:00:00Z",
                                 "updated_ts": "2026-08-01T00:00:00Z"}) + "\n")
        assert (99, "id") in o.load_registry(), "legacy rows must still load"
        assert o.registry_get(None, "id", ep_id=99)["source"] == "asr"
    finally:
        o.REGISTRY_FILE = saved
    print("PASS ladder_registry")


def test_ladder_clean_ass_text():
    """{\\an8} override blocks and <font> tags stripped; \\N preserved as a
    line break; blank lines collapse."""
    src = '<font size="75">{\\an8}(フリーレン)\\N王都が見えてきたね</font>'
    out = o.clean_ass_text(src)
    assert "\\an8" not in out and "font" not in out, out
    assert "(フリーレン)\n王都が見えてきたね" == out, out
    assert o.clean_ass_text("{\\an8}♪~") == "♪~"
    assert o.clean_ass_text("a\\nb") == "a\nb"
    print("PASS ladder_clean_ass_text")


def test_ladder_assess_gates():
    """assess_source_file: min cues, min chars, CJK/latin ratio, span
    tolerance all enforced; ok result carries cleaned cues + source_hash."""
    import tempfile

    d = tempfile.mkdtemp()
    p = os.path.join(d, "sub.srt")
    cfg = build_cfg()
    cfg["LADDER_MIN_CUES"] = "40"
    cfg["LADDER_MIN_CHARS"] = "1500"
    cfg["LADDER_MIN_CJK"] = "0.6"
    cfg["LADDER_SPAN_TOLERANCE"] = "0.15"

    # too few cues
    _write_srt(p, [(i * 1000, i * 1000 + 800, "テストの台詞です。" + "あいうえお") for i in range(10)])
    v = o.assess_source_file(cfg, p, "jpn", 60.0)
    assert not v["ok"] and "cues" in v["reason"], v
    # not enough chars
    _write_srt(p, [(i * 1000, i * 1000 + 800, "テスト") for i in range(60)])
    v = o.assess_source_file(cfg, p, "jpn", 60.0)
    assert not v["ok"] and "chars" in v["reason"], v
    # low CJK ratio (mostly latin) rejected for jpn
    lines = [(i * 1000, i * 1000 + 800, "This is an english subtitle line for the gate test. " + "a" * 20) for i in range(80)]
    _write_srt(p, lines)
    v = o.assess_source_file(cfg, p, "jpn", 80.0)
    assert not v["ok"] and "ratio" in v["reason"], v
    # latin ratio passes for eng
    v = o.assess_source_file(cfg, p, "eng", 80.0)
    assert v["ok"], v
    # span mismatch rejected
    _write_srt(p, [(i * 1000, i * 1000 + 800, "テストの台詞です。あいうえおかきくけこさしすせそ。") for i in range(60)])
    v = o.assess_source_file(cfg, p, "jpn", 600.0)
    assert not v["ok"] and "span" in v["reason"], v
    # everything passes at the right duration; span is last cue END
    v = o.assess_source_file(cfg, p, "jpn", 60.8)
    assert v["ok"], v
    assert len(v["cues"]) == 60 and all("{" not in c["text"] for c in v["cues"])
    assert isinstance(v["source_hash"], str) and len(v["source_hash"]) == 64, v["source_hash"]
    print("PASS ladder_assess_gates")


def test_ladder_detect_sidecar():
    """detect_ladder_source picks the external jpn sidecar over ASR; a
    gate-failing sidecar falls through to eng then asr; kind is reported."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    jpn = os.path.join(d, "Ep.jpn.srt")
    eng = os.path.join(d, "Ep.eng.srt")
    cfg = build_cfg()
    cfg["TARGET_LANGS"] = ["id", "en"]
    cfg["TMP_DIR"] = d

    # passing jpn sidecar -> jpn
    _write_srt(jpn, [(i * 1000, i * 1000 + 800, "テストの台詞です。あいうえおかきくけこさしすせそ。") for i in range(60)])
    v = o.detect_ladder_source(cfg, mkv, "id", d, ep_id=1, series_id=2)
    assert v["kind"] == "jpn" and v["source_path"] == jpn, v
    assert v["cues"] and len(v["cues"]) == 60

    # gate-failing jpn (too few cues) -> eng sidecar
    _write_srt(jpn, [(i * 1000, i * 1000 + 800, "テスト") for i in range(5)])
    _write_srt(eng, [(i * 1000, i * 1000 + 800, "This is a perfectly good english subtitle line for the test.") for i in range(60)])
    v = o.detect_ladder_source(cfg, mkv, "id", d, ep_id=1, series_id=2)
    assert v["kind"] == "eng", v

    # no usable sidecar -> asr
    os.remove(jpn)
    os.remove(eng)
    v = o.detect_ladder_source(cfg, mkv, "id", d, ep_id=1, series_id=2)
    assert v["kind"] == "asr" and v["source_path"] is None, v
    print("PASS ladder_detect_sidecar")


def test_ladder_marker_fallback():
    """srt_has_ai_marker only accepts the marker in the FIRST cue;
    sub_is_ai_owned trusts the registry, falls back to the marker."""
    import tempfile

    d = tempfile.mkdtemp()
    sub = os.path.join(d, "Ep.id.srt")
    _write_srt(sub, [(500, 1500, o.AI_MARKER), (2000, 3000, "Dia menoleh.")])
    assert o.srt_has_ai_marker(sub)
    sub2 = os.path.join(d, "Ep2.id.srt")
    _write_srt(sub2, [(2000, 3000, "Dia menoleh."), (3000, 4000, o.AI_MARKER)])
    assert not o.srt_has_ai_marker(sub2), "marker must be first cue only"
    media = os.path.join(d, "Ep.mkv")
    open(media, "w").close()
    registry = {}
    assert o.sub_is_ai_owned(registry, 1, "id", media), "marker fallback -> owned"
    registry[(1, "id")] = {"source": "jpn"}
    assert o.sub_is_ai_owned(registry, 1, "id", media)
    foreign = os.path.join(d, "Ep3.id.srt")
    _write_srt(foreign, [(2000, 3000, "Foreign sub without marker.")])
    media3 = os.path.join(d, "Ep3.mkv")
    open(media3, "w").close()
    assert not o.sub_is_ai_owned({}, 3, "id", media3), "no registry + no marker -> foreign"
    print("PASS ladder_marker_fallback")


def test_ladder_audio_id():
    """audio_stream_signature is stable for identical streams, distinct when
    any signature dimension changes (format duration included), and never
    contains the episode id."""
    s1 = [{"index": 1, "codec_name": "aac", "tags": {"language": "jpn"},
           "channel_layout": "stereo", "duration": "1500.0"}]
    s2 = [{"index": 1, "codec_name": "aac", "tags": {"language": "jpn"},
           "channel_layout": "stereo", "duration": "1500.0"}]
    s3 = [{"index": 1, "codec_name": "aac", "tags": {"language": "jpn"},
           "channel_layout": "5.1", "duration": "1500.0"}]
    a1 = o.audio_stream_signature(s1)
    assert a1 == o.audio_stream_signature(s2)
    assert a1 != o.audio_stream_signature(s3)
    assert len(a1) == 32 and all(c in "0123456789abcdef" for c in a1)
    assert "42" not in a1
    s4 = o._AudioStreams(
        [{"index": 1, "codec_name": "aac", "tags": {"language": "jpn"},
          "channel_layout": "stereo", "duration": "1500.0"}]
    )
    s4.format_duration = 1500.0
    assert o.audio_stream_signature(s4) != o.audio_stream_signature(s1), (
        "format duration must feed the signature"
    )
    print("PASS ladder_audio_id")


def test_ladder_upgrade_guards():
    """run_upgrades: cooldown skips fresh rows, refine-history skips refined,
    source-hash dedup skips identical, budget caps upgrades, foreign subs
    never upgraded."""
    import tempfile

    d = tempfile.mkdtemp()
    saved_reg = o.REGISTRY_FILE
    saved_refine = o.REFINE_STATE_FILE
    o.REGISTRY_FILE = os.path.join(d, "registry.jsonl")
    o.REFINE_STATE_FILE = os.path.join(d, "refine.jsonl")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def row(ep_id, lang, source, updated, source_hash=""):
        return {"episode_id": ep_id, "lang": lang, "source": source,
                "source_path": "", "source_hash": source_hash,
                "created_ts": updated, "updated_ts": updated}

    rows = [
        row(10, "id", "asr", old),              # fresh-worthy, budget upgrade
        row(11, "id", "asr", now),              # cooldown -> skip
        row(12, "id", "asr", old),              # refined -> skip
        row(13, "id", "eng", old, source_hash="same"),  # hash dedup -> skip
    ]
    with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(o.REFINE_STATE_FILE, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ep_id": 12, "lang": "id", "status": "done"}, ensure_ascii=False) + "\n")

    media = os.path.join(d, "Ep.mkv")
    open(media, "w").close()

    saved_get_episode = o.get_episode
    saved_detect = o.detect_ladder_source
    saved_process = o.process_ladder
    o.get_episode = lambda cfg, eid: {
        "hasFile": True,
        "title": "Ep",
        "episodeFile": {"path": media},
        "seriesId": 1,
        "seasonNumber": 1,
        "episodeNumber": eid,
    }
    o.detect_ladder_source = lambda *a, **k: {
        "kind": "jpn", "source_path": "/jpn.srt",
        "source_hash": "same" if len(a) > 4 and a[4] == 13 else "newhash",
        "cues": [{"start": "00:00:00,000", "end": "00:00:01,000", "text": "x"}],
        "duration_s": 1.0, "tmp": False,
    }
    processed = []

    def fake_process_ladder(cfg, key, ep_id, lang, series, tag, source, info, prior_cache=None):
        o.registry_upsert(ep_id, lang, "jpn", source_path="/jpn.srt", source_hash="newhash")
        processed.append((ep_id, lang))
        return "done"

    o.process_ladder = fake_process_ladder
    try:
        cfg = build_cfg()
        cfg["TMP_DIR"] = d
        cfg["LADDER_UPGRADE_BUDGET"] = "1"
        cfg["LADDER_COOLDOWN_H"] = "24"
        stats = o.run_upgrades(cfg, "k")
        assert stats["upgraded"] == 1, stats
        assert processed == [(10, "id")], processed
        # second call: ep 10 already jpn in registry -> nothing to upgrade
        stats2 = o.run_upgrades(cfg, "k")
        assert stats2["upgraded"] == 0, stats2
    finally:
        o.REGISTRY_FILE = saved_reg
        o.REFINE_STATE_FILE = saved_refine
        o.get_episode = saved_get_episode
        o.detect_ladder_source = saved_detect
        o.process_ladder = saved_process
    print("PASS ladder_upgrade_guards")


def _run_result(rc, stdout="", stderr=""):
    class R:
        pass
    r = R()
    r.returncode = rc
    r.stdout = stdout
    r.stderr = stderr
    return r


def test_ladder_align_skipped_for_embedded():
    """Trusted sidecar skips the gate: the registry row for (stem, jpn) is
    source_kind 'embedded' with an audio_id matching the current video and a
    source_hash matching the on-disk sidecar content (tdarr-extracted,
    aligned by construction): no ffs/ffmpeg subprocess is ever invoked."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    jpn = os.path.join(d, "Ep.jpn.srt")
    _write_srt(jpn, [(i * 1000, i * 1000 + 800, "テストの台詞です。あいうえおかきくけこさしすせそ。") for i in range(60)])
    saved_reg = o.REGISTRY_FILE
    saved_run = o.subprocess.run
    saved_extract = o.extract_embedded_subtitle
    saved_bazarr = o.bazarr_jpn_candidate
    saved_probe = o.probe_audio
    fake_streams = o._AudioStreams(
        [{"index": 1, "codec_name": "aac", "tags": {"language": "jpn"},
          "channel_layout": "stereo", "duration": "60.0"}]
    )
    fake_streams.format_duration = 60.0
    o.probe_audio = lambda path: fake_streams
    o.REGISTRY_FILE = os.path.join(d, "registry.jsonl")
    with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "stem": os.path.splitext(mkv)[0], "lang": "jpn", "episode_id": 1,
            "source": "embedded", "source_kind": "embedded",
            "source_path": jpn, "source_hash": o.file_sha256(jpn),
            "audio_id": o.audio_stream_signature(fake_streams),
            "created_ts": "2026-08-01T00:00:00Z", "updated_ts": "2026-08-01T00:00:00Z",
        }) + "\n")
    o.extract_embedded_subtitle = lambda *a, **k: False
    o.bazarr_jpn_candidate = lambda *a, **k: None
    calls = []

    def spy(cmd, **kw):
        calls.append(cmd[0])
        return _run_result(1)
    o.subprocess.run = spy
    try:
        cfg = build_cfg()
        cfg["TMP_DIR"] = d
        cfg["ALIGN_ENABLED"] = "true"
        v = o.detect_ladder_source(cfg, mkv, "id", d, ep_id=1, series_id=2)
        assert v["kind"] == "jpn" and v["source_path"] == jpn, v
        assert v.get("align_tmp") is None, v
        assert "ffs" not in calls and "ffmpeg" not in calls, calls
    finally:
        o.REGISTRY_FILE = saved_reg
        o.subprocess.run = saved_run
        o.extract_embedded_subtitle = saved_extract
        o.bazarr_jpn_candidate = saved_bazarr
        o.probe_audio = saved_probe
    print("PASS ladder_align_skipped_for_embedded")


def test_webhook_registers_extraction():
    """extract_subtitle_sidecars registers a provenance row per written
    sidecar: (stem, lang) with source_kind 'embedded', audio_id = signature
    of the video it was extracted from, source_hash = content hash of the
    just-written sidecar, and no episode id (the webhook only knows the file
    path, never Sonarr ids)."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    saved_reg = o.REGISTRY_FILE
    saved_run = o.subprocess.run
    saved_streams = o._subtitle_streams
    saved_probe = o.probe_audio
    o.REGISTRY_FILE = os.path.join(d, "registry.jsonl")
    fake_streams = o._AudioStreams(
        [{"index": 1, "codec_name": "aac", "tags": {"language": "jpn"},
          "channel_layout": "stereo", "duration": "60.0"}]
    )
    fake_streams.format_duration = 60.0
    o.probe_audio = lambda path: fake_streams
    o._subtitle_streams = lambda path: [
        {"index": 3, "codec_type": "subtitle", "codec_name": "ass",
         "tags": {"language": "jpn", "NUMBER_OF_FRAMES": "500"}},
        {"index": 4, "codec_type": "subtitle", "codec_name": "ass",
         "tags": {"language": "eng", "NUMBER_OF_FRAMES": "400"}},
    ]

    def fake_ffmpeg(cmd, **kw):
        if cmd[0] == "ffmpeg":
            idx = cmd[cmd.index("-map") + 1].split(":")[1]
            with open(cmd[-1], "w", encoding="utf-8") as fh:
                fh.write(f"1\n00:00:00,000 --> 00:00:01,000\nsub {idx}\n")
            return _run_result(0)
        return _run_result(1)
    o.subprocess.run = fake_ffmpeg
    try:
        o.extract_subtitle_sidecars(mkv, tdarr_id="t1")
        stem = os.path.splitext(mkv)[0]
        row_jpn = o.registry_get(stem, "jpn")
        row_eng = o.registry_get(stem, "eng")
        assert row_jpn and row_eng, (row_jpn, row_eng)
        assert row_jpn["episode_id"] is None and row_eng["episode_id"] is None
        for row, lang in ((row_jpn, "jpn"), (row_eng, "eng")):
            assert row["source_kind"] == "embedded", row
            assert row["source"] == "embedded", row
            assert row["audio_id"] == o.audio_stream_signature(fake_streams), row
            sidecar = os.path.join(d, f"Ep.{lang}.srt")
            assert os.path.isfile(sidecar), sidecar
            assert row["source_hash"] == o.file_sha256(sidecar), row
        reg = o.load_registry()
        assert (stem, "jpn") in reg and (stem, "eng") in reg, set(reg)
    finally:
        o.REGISTRY_FILE = saved_reg
        o.subprocess.run = saved_run
        o._subtitle_streams = saved_streams
        o.probe_audio = saved_probe
    print("PASS webhook_registers_extraction")


def test_webhook_dedup_skips_unchanged():
    """Webhook dedup: when registry rows exist with matching audio_id and
    matching on-disk content hashes, re-extraction is skipped entirely (no
    subprocess at all — the rescan storm costs only the header-only audio
    probe). A stale audio_id (Sonarr upgrade) or a missing row re-runs the
    extraction and re-registers the row."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    stem = os.path.splitext(mkv)[0]
    fake_streams = o._AudioStreams(
        [{"index": 1, "codec_name": "aac", "tags": {"language": "jpn"},
          "channel_layout": "stereo", "duration": "60.0"}]
    )
    fake_streams.format_duration = 60.0
    saved_reg = o.REGISTRY_FILE
    saved_run = o.subprocess.run
    saved_probe = o.probe_audio
    saved_streams = o._subtitle_streams
    o.REGISTRY_FILE = os.path.join(d, "registry.jsonl")
    o.probe_audio = lambda path: fake_streams
    jpn = os.path.join(d, "Ep.jpn.srt")
    eng = os.path.join(d, "Ep.eng.srt")
    _write_srt(jpn, [(i * 1000, i * 1000 + 800, "テストの台詞です。あいうえお") for i in range(3)])
    _write_srt(eng, [(i * 1000, i * 1000 + 800, "A test english line for the dedup gate.") for i in range(3)])

    def row(lang, hash_, audio):
        return {"stem": stem, "lang": lang, "episode_id": None, "source": "embedded",
                "source_kind": "embedded", "source_path": f"{stem}.{lang}.srt",
                "source_hash": hash_, "audio_id": audio,
                "created_ts": "2026-08-01T00:00:00Z", "updated_ts": "2026-08-01T00:00:00Z"}

    o.REGISTRY_FILE = os.path.join(d, "registry.jsonl")
    with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
        for lang, p in (("jpn", jpn), ("eng", eng)):
            fh.write(json.dumps(row(lang, o.file_sha256(p),
                                    o.audio_stream_signature(fake_streams))) + "\n")
    calls = []

    def spy(cmd, **kw):
        calls.append(cmd[0])
        return _run_result(1)
    o.subprocess.run = spy
    try:
        # everything unchanged -> full skip, zero subprocesses
        o.extract_subtitle_sidecars(mkv, tdarr_id="t1")
        assert not calls, calls
        # stale audio_id (video upgraded): the row no longer matches ->
        # re-extract (ffmpeg runs) and the latest row is re-registered
        with open(o.REGISTRY_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row("jpn", o.file_sha256(jpn), "old-audio-id")) + "\n")
        ffmpeg_calls = []

        def fake_ffmpeg(cmd, **kw):
            if cmd[0] == "ffmpeg":
                ffmpeg_calls.append(cmd)
                with open(cmd[-1], "w", encoding="utf-8") as fh:
                    fh.write("1\n00:00:00,000 --> 00:00:01,000\nテスト\n")
                return _run_result(0)
            return _run_result(1)
        o.subprocess.run = fake_ffmpeg
        o._subtitle_streams = lambda path: [
            {"index": 3, "codec_type": "subtitle", "codec_name": "ass",
             "tags": {"language": "jpn", "NUMBER_OF_FRAMES": "100"}},
        ]
        o.extract_subtitle_sidecars(mkv, tdarr_id="t2")
        assert len(ffmpeg_calls) == 1, ffmpeg_calls
        assert o.registry_get(stem, "jpn")["audio_id"] == o.audio_stream_signature(fake_streams)
        # no row at all -> extraction runs and registers
        with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
            fh.write("")
        ffmpeg_calls.clear()
        o.extract_subtitle_sidecars(mkv, tdarr_id="t3")
        assert len(ffmpeg_calls) == 1, ffmpeg_calls
        r = o.registry_get(stem, "jpn")
        assert r and r["audio_id"] == o.audio_stream_signature(fake_streams), r
        assert r["source_hash"] == o.file_sha256(jpn), r
    finally:
        o.REGISTRY_FILE = saved_reg
        o.subprocess.run = saved_run
        o.probe_audio = saved_probe
        o._subtitle_streams = saved_streams
    print("PASS webhook_dedup_skips_unchanged")


def test_ladder_clobbered_sidecar_retimed():
    """Clobbered sidecar: the row says embedded and the audio matches, but the
    on-disk sidecar content hash differs from the row's source_hash (Bazarr
    overwrote the webhook extraction) -> NOT trusted, the re-timing gate runs;
    when the ASR anchor probe fails the sidecar is used as-is (never a
    mis-timed reject) and the clobbered reason is logged."""
    import contextlib
    import io
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    jpn = os.path.join(d, "Ep.jpn.srt")
    _write_srt(jpn, [(i * 1000, i * 1000 + 800, "テストの台詞です。あいうえおかきくけこさしすせそ。") for i in range(60)])
    saved_reg = o.REGISTRY_FILE
    saved_run = o.subprocess.run
    saved_extract = o.extract_embedded_subtitle
    saved_bazarr = o.bazarr_jpn_candidate
    saved_probe = o.probe_audio
    saved_cache_get = o.asr_cache_get
    fake_streams = o._AudioStreams(
        [{"index": 1, "codec_name": "aac", "tags": {"language": "jpn"},
          "channel_layout": "stereo", "duration": "60.0"}]
    )
    fake_streams.format_duration = 60.0
    o.probe_audio = lambda path: fake_streams
    o.REGISTRY_FILE = os.path.join(d, "registry.jsonl")
    with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "stem": os.path.splitext(mkv)[0], "lang": "jpn", "episode_id": 1,
            "source": "embedded", "source_kind": "embedded",
            "source_path": jpn, "source_hash": "0" * 64,
            "audio_id": o.audio_stream_signature(fake_streams),
            "created_ts": "2026-08-01T00:00:00Z", "updated_ts": "2026-08-01T00:00:00Z",
        }) + "\n")
    o.extract_embedded_subtitle = lambda *a, **k: False
    o.bazarr_jpn_candidate = lambda *a, **k: None
    o.asr_cache_get = lambda *a, **k: None
    calls = []

    def spy(cmd, **kw):
        calls.append(cmd[0])
        return _run_result(1)
    o.subprocess.run = spy
    buf = io.StringIO()
    try:
        cfg = build_cfg()
        cfg["TMP_DIR"] = d
        cfg["ALIGN_ENABLED"] = "true"
        with contextlib.redirect_stdout(buf):
            v = o.detect_ladder_source(cfg, mkv, "id", d, ep_id=1, series_id=2)
        assert v["kind"] == "jpn", v
        assert "ffs" not in calls, calls
        assert "clobbered" in buf.getvalue(), buf.getvalue()
        assert v.get("align_stats", {}).get("method") is None, v
    finally:
        o.REGISTRY_FILE = saved_reg
        o.subprocess.run = saved_run
        o.extract_embedded_subtitle = saved_extract
        o.bazarr_jpn_candidate = saved_bazarr
        o.probe_audio = saved_probe
        o.asr_cache_get = saved_cache_get
    print("PASS ladder_clobbered_sidecar_retimed")


def test_ladder_stale_sidecar_retimed():
    """Stale sidecar: the row says embedded and the on-disk hash matches, but
    the row's audio_id differs from the CURRENT video's audio signature
    (sidecar left behind by a Sonarr upgrade) -> NOT trusted, the re-timing
    gate runs; an anchor-probe failure uses the sidecar as-is and the stale
    reason is logged."""
    import contextlib
    import io
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    jpn = os.path.join(d, "Ep.jpn.srt")
    _write_srt(jpn, [(i * 1000, i * 1000 + 800, "テストの台詞です。あいうえおかきくけこさしすせそ。") for i in range(60)])
    saved_reg = o.REGISTRY_FILE
    saved_run = o.subprocess.run
    saved_extract = o.extract_embedded_subtitle
    saved_bazarr = o.bazarr_jpn_candidate
    saved_probe = o.probe_audio
    saved_cache_get = o.asr_cache_get
    fake_streams = o._AudioStreams(
        [{"index": 1, "codec_name": "aac", "tags": {"language": "jpn"},
          "channel_layout": "stereo", "duration": "60.0"}]
    )
    fake_streams.format_duration = 60.0
    o.probe_audio = lambda path: fake_streams
    o.REGISTRY_FILE = os.path.join(d, "registry.jsonl")
    with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "stem": os.path.splitext(mkv)[0], "lang": "jpn", "episode_id": 1,
            "source": "embedded", "source_kind": "embedded",
            "source_path": jpn, "source_hash": o.file_sha256(jpn),
            "audio_id": "stale-audio-id",
            "created_ts": "2026-08-01T00:00:00Z", "updated_ts": "2026-08-01T00:00:00Z",
        }) + "\n")
    o.extract_embedded_subtitle = lambda *a, **k: False
    o.bazarr_jpn_candidate = lambda *a, **k: None
    o.asr_cache_get = lambda *a, **k: None
    calls = []

    def spy(cmd, **kw):
        calls.append(cmd[0])
        return _run_result(1)
    o.subprocess.run = spy
    buf = io.StringIO()
    try:
        cfg = build_cfg()
        cfg["TMP_DIR"] = d
        cfg["ALIGN_ENABLED"] = "true"
        with contextlib.redirect_stdout(buf):
            v = o.detect_ladder_source(cfg, mkv, "id", d, ep_id=1, series_id=2)
        assert v["kind"] == "jpn", v
        assert "ffs" not in calls, calls
        assert "stale" in buf.getvalue(), buf.getvalue()
        assert v.get("align_stats", {}).get("method") is None, v
    finally:
        o.REGISTRY_FILE = saved_reg
        o.subprocess.run = saved_run
        o.extract_embedded_subtitle = saved_extract
        o.bazarr_jpn_candidate = saved_bazarr
        o.probe_audio = saved_probe
        o.asr_cache_get = saved_cache_get
    print("PASS ladder_stale_sidecar_retimed")


def test_ladder_process_preserves_source_kind():
    """process_ladder upsert: using an on-disk sidecar whose registry row
    still matches (source_kind 'embedded', hash match) PRESERVES the row's
    source_kind instead of flipping it to 'external'; a clobbered sidecar
    (hash mismatch) records 'external'; a tmp extraction records 'embedded'
    without clobbering the on-disk sidecar's own row."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    eng = os.path.join(d, "Ep.eng.srt")
    _write_srt(eng, [(i * 1000, i * 1000 + 800, "A perfectly good english subtitle line for the preserve gate.") for i in range(60)])
    stem = os.path.splitext(mkv)[0]
    cues = [{"start": i * 1000, "end": i * 1000 + 800,
             "text": "A perfectly good english subtitle line for the preserve gate."}
            for i in range(60)]
    saved_reg = o.REGISTRY_FILE
    saved_state = o.STATE_FILE
    saved_upload = o.upload_srt
    saved_refresh = o.jellyfin_refresh
    o.REGISTRY_FILE = os.path.join(d, "registry.jsonl")
    o.STATE_FILE = os.path.join(d, "state.jsonl")
    o.upload_srt = lambda *a, **k: 204
    o.jellyfin_refresh = lambda *a, **k: None
    with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "stem": stem, "lang": "eng", "episode_id": None,
            "source": "embedded", "source_kind": "embedded",
            "source_path": eng, "source_hash": o.file_sha256(eng),
            "audio_id": "a1b2c3",
            "created_ts": "2026-08-01T00:00:00Z", "updated_ts": "2026-08-01T00:00:00Z",
        }) + "\n")
    info = {"episodeFile": {"path": mkv}, "seriesId": 1, "title": "Ep"}
    cfg = build_cfg()
    try:
        # on-disk sidecar, registry hash matches -> source_kind preserved
        source = {"kind": "eng", "source_path": eng,
                  "source_hash": o.file_sha256(eng), "cues": cues,
                  "duration_s": 60.0, "tmp": False}
        status = o.process_ladder(cfg, "k", 5, "en", "Series", "S1E5", source, info)
        assert status == "done", status
        row = o.registry_get(stem, "en")
        assert row["source_kind"] == "embedded", row
        assert row["source"] == "eng" and row["episode_id"] == 5, row
        assert o.registry_get(stem, "eng")["source_kind"] == "embedded", \
            "the on-disk sidecar's own row must not be clobbered"
        # clobbered sidecar (content changed since the row) -> external
        _write_srt(eng, [(i * 1000, i * 1000 + 800, "A different english subtitle line that clobbers the extraction.") for i in range(60)])
        source2 = {"kind": "eng", "source_path": eng,
                   "source_hash": o.file_sha256(eng), "cues": cues,
                   "duration_s": 60.0, "tmp": False}
        status = o.process_ladder(cfg, "k", 5, "en", "Series", "S1E5", source2, info)
        assert status == "done", status
        row2 = o.registry_get(stem, "en")
        assert row2["source_kind"] == "external", row2
        # tmp extraction -> embedded, and the on-disk row stays untouched
        tmp_src = os.path.join(d, "ladder_tmp.srt")
        _write_srt(tmp_src, [(i * 1000, i * 1000 + 800, "A tmp extracted english line for the preserve gate.") for i in range(60)])
        source3 = {"kind": "eng", "source_path": tmp_src,
                   "source_hash": o.file_sha256(tmp_src), "cues": cues,
                   "duration_s": 60.0, "tmp": True}
        status = o.process_ladder(cfg, "k", 5, "en", "Series", "S1E5", source3, info)
        assert status == "done", status
        row3 = o.registry_get(stem, "en")
        assert row3["source_kind"] == "embedded", row3
        assert o.registry_get(stem, "eng")["source_kind"] == "embedded"
    finally:
        o.REGISTRY_FILE = saved_reg
        o.STATE_FILE = saved_state
        o.upload_srt = saved_upload
        o.jellyfin_refresh = saved_refresh
    print("PASS ladder_process_preserves_source_kind")


def test_ladder_retime_reject_falls_through():
    """An untrusted external sidecar whose ASR anchors come back EMPTY is
    rejected by the retime gate and the ladder falls through to the next rung
    (asr here); no temp files are left behind."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    jpn = os.path.join(d, "Ep.jpn.srt")
    _write_srt(jpn, [(i * 1000, i * 1000 + 800, "テストの台詞です。あいうえおかきくけこさしすせそ。") for i in range(60)])
    saved_reg = o.REGISTRY_FILE
    saved_run = o.subprocess.run
    saved_extract = o.extract_embedded_subtitle
    saved_bazarr = o.bazarr_jpn_candidate
    saved_anchor = o._retime_anchor_asr
    o.REGISTRY_FILE = os.path.join(d, "registry.jsonl")  # empty: unregistered
    o.extract_embedded_subtitle = lambda *a, **k: False
    o.bazarr_jpn_candidate = lambda *a, **k: None
    o._retime_anchor_asr = lambda *a, **k: ([], "ja")
    calls = []

    def spy(cmd, **kw):
        calls.append(cmd[0])
        return _run_result(1)
    o.subprocess.run = spy
    try:
        cfg = build_cfg()
        cfg["TMP_DIR"] = d
        v = o.detect_ladder_source(cfg, mkv, "id", d, ep_id=1, series_id=2)
        assert v["kind"] == "asr" and v["source_path"] is None, v
        assert "ffs" not in calls, calls
        leftovers = [f for f in os.listdir(d) if f.startswith(("align_", "retime_"))]
        assert not leftovers, leftovers
    finally:
        o.REGISTRY_FILE = saved_reg
        o.subprocess.run = saved_run
        o.extract_embedded_subtitle = saved_extract
        o.bazarr_jpn_candidate = saved_bazarr
        o._retime_anchor_asr = saved_anchor
    print("PASS ladder_retime_reject_falls_through")


def _run_pass_single_episode(tmp_dir, media_path):
    """Drive one full run_pass() pass with a single wanted Indonesian episode
    and every I/O boundary monkeypatched (no HTTP, no ffmpeg, no ASR).
    Returns (stats, mocks, state_file)."""
    cfg = build_cfg()
    cfg["TARGET_LANGS"] = ["id"]
    cfg["TRANSLATE_API_KEY"] = "test-key"
    cfg["MAX_EPS_PER_RUN"] = 8
    cfg["TMP_DIR"] = tmp_dir
    wanted = {
        "total": 1,
        "data": [
            {
                "sonarrEpisodeId": 101,
                "seriesTitle": "TestShow",
                "missing_subtitles": [{"code2": "id"}],
            }
        ],
    }
    info = {
        "hasFile": True,
        "seasonNumber": 1,
        "episodeNumber": 2,
        "seriesId": 99,
        "episodeFile": {"path": media_path},
    }
    mocks = {
        "get_wanted_calls": [],
        "probe": [],
        "choose": [],
        "extract": [],
        "asr": [],
        "submit": [],
        "state_writes": [],
        "log": [],
    }
    saved = {}

    def save(name):
        saved[name] = getattr(o, name)

    for n in (
        "load_config",
        "consume_actions",
        "load_state",
        "get_wanted",
        "parse_exclusions",
        "get_episode",
        "probe_audio",
        "choose_source",
        "detect_ladder_source",
        "asr_cache_get",
        "extract_wav",
        "asr_cues",
        "asr_cache_put",
        "process_after_asr",
        "run_upgrades",
        "log",
        "notify_hermes",
        "halt_on_error",
        "append_state",
    ):
        save(n)
    o.load_config = lambda: cfg
    o.consume_actions = lambda cfg_: set()
    o.load_state = lambda: []
    o.get_wanted = lambda cfg_: mocks["get_wanted_calls"].append(1) or wanted
    o.parse_exclusions = lambda: set()
    o.get_episode = lambda cfg_, ep_id: info
    o.probe_audio = lambda path: mocks["probe"].append(path) or [
        {
            "index": 1,
            "codec_name": "aac",
            "tags": {"language": "jpn"},
            "channels": 2,
            "duration": "100.0",
        }
    ]
    o.choose_source = (
        lambda streams, lang: mocks["choose"].append(lang)
        or {
            "stream_index": 1,
            "asr_lang": "ja",
            "needs_translate": True,
            "src_lang": "jpn",
        }
    )
    o.detect_ladder_source = lambda *a, **k: {"kind": "asr", "source_path": None}
    o.asr_cache_get = lambda *a: None
    o.extract_wav = lambda path, idx, out: mocks["extract"].append((path, idx)) or open(out, "w").close()
    o.asr_cues = (
        lambda cfg_, wav, lang: mocks["asr"].append(lang)
        or [{"start": 0, "end": 1000, "text": "Halo dunia."}]
    )
    o.asr_cache_put = lambda *a: None
    o.process_after_asr = lambda *a, **k: mocks["submit"].append(a) or "done"
    o.run_upgrades = lambda *a, **k: {"upgraded": 0, "checked": 0}
    o.log = lambda msg: mocks["log"].append(msg)
    o.notify_hermes = lambda *a, **k: None
    o.halt_on_error = lambda *a, **k: None
    o.append_state = lambda entry: mocks["state_writes"].append(entry)
    saved_state_file = o.STATE_FILE
    state_file = os.path.join(tmp_dir, "state.jsonl")
    o.STATE_FILE = state_file
    try:
        stats = o.run_pass()
    finally:
        for name, val in saved.items():
            setattr(o, name, val)
        o.STATE_FILE = saved_state_file
    return stats, mocks, state_file


def test_pass_skip_existing_id_sidecar():
    """wanted-pass skips an episode whose {stem}.id.srt already exists on
    disk (Bazarr grabbed it): no probe/ASR/translate work, no state write."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    open(os.path.join(d, "Ep.id.srt"), "w").close()
    stats, mocks, state_file = _run_pass_single_episode(d, mkv)
    assert stats["skipped"] == 1, stats
    assert stats["done"] == 0 and stats["failed"] == 0, stats
    assert not mocks["probe"], "probe_audio must not run for a skipped episode"
    assert not mocks["choose"] and not mocks["extract"] and not mocks["asr"]
    assert not mocks["submit"], "process_after_asr must not run"
    assert not mocks["state_writes"], "no state write on skip"
    assert not os.path.exists(state_file), "state.jsonl must not be created"
    assert any(
        "target-lang sidecar exists (Ep.id.srt)" in line
        for line in mocks["log"]
    ), mocks["log"]
    print("PASS pass_skip_existing_id_sidecar")


def test_pass_skip_existing_id_hi_sidecar():
    """An existing {stem}.id.hi.srt also skips ASR (rule includes non-HI and
    HI variants alike)."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    open(os.path.join(d, "Ep.id.hi.srt"), "w").close()
    stats, mocks, state_file = _run_pass_single_episode(d, mkv)
    assert stats["skipped"] == 1, stats
    assert stats["done"] == 0 and stats["failed"] == 0, stats
    assert not mocks["probe"] and not mocks["asr"] and not mocks["submit"]
    assert not mocks["state_writes"]
    assert not os.path.exists(state_file)
    assert any(
        "target-lang sidecar exists (Ep.id.hi.srt)" in line
        for line in mocks["log"]
    ), mocks["log"]
    print("PASS pass_skip_existing_id_hi_sidecar")


def test_pass_no_id_sidecar_processes():
    """jpn/eng sidecars on disk are NOT target-language: the episode is still
    processed normally (probe + ASR path run, done=1)."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    for name in ("Ep.jpn.srt", "Ep.eng.srt"):
        open(os.path.join(d, name), "w").close()
    stats, mocks, state_file = _run_pass_single_episode(d, mkv)
    assert stats["done"] == 1 and stats["skipped"] == 0, stats
    assert len(mocks["probe"]) == 1 and len(mocks["extract"]) == 1
    assert mocks["asr"] == ["ja"], mocks["asr"]
    assert len(mocks["submit"]) == 1
    assert not mocks["state_writes"], "normal pass may not write state in dry run"
    print("PASS pass_no_id_sidecar_processes")


def test_target_sidecar_exists_patterns():
    """The skip contract: {stem}.id/.ind srt with optional .hi, case-
    insensitive; jpn/eng/other files never match."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Show.S01.E02.mkv")
    open(mkv, "w").close()
    for name in (
        "Show.S01.E02.id.srt",
        "Show.S01.E02.id.hi.srt",
        "Show.S01.E02.ind.srt",
        "Show.S01.E02.ind.hi.srt",
        "Show.S01.E02.ID.SRT",
        "Show.S01.E02.Id.Hi.Srt",
    ):
        open(os.path.join(d, name), "w").close()
        assert o.target_sidecar_exists(mkv) == os.path.join(d, name), name
        os.remove(os.path.join(d, name))
    for name in (
        "Show.S01.E02.jpn.srt",
        "Show.S01.E02.eng.srt",
        "Show.S01.E02.eng.hi.srt",
        "Show.S01.E02.idx",
        "Show.S01.E02.id.srtx",
        "Show.S01.E02id.srt",
        "Show.S01.E03.id.srt",
    ):
        open(os.path.join(d, name), "w").close()
    assert o.target_sidecar_exists(mkv) is None, "non-target files must not match"
    print("PASS target_sidecar_exists_patterns")

def _run_pass_regen(
    tmp_dir,
    infos,
    wanted=None,
    state=None,
    regen=False,
    max_eps=8,
    target_langs=None,
    registry_rows=None,
    sidecar=None,
):
    """Drive run_pass() with every I/O boundary monkeypatched (no HTTP,
    ffmpeg, or ASR). infos maps ep_id -> get_episode() response; each info
    must carry a real media file path. Returns (stats, mocks)."""
    cfg = build_cfg()
    cfg["TARGET_LANGS"] = target_langs or ["id"]
    cfg["TRANSLATE_API_KEY"] = "test-key"
    cfg["MAX_EPS_PER_RUN"] = max_eps
    cfg["TMP_DIR"] = tmp_dir
    cfg["REGEN_LIBRARY"] = regen
    mocks = {
        "get_wanted_calls": [],
        "probe": [],
        "submit": [],
        "sidecar_calls": [],
        "state_writes": [],
        "log": [],
    }
    saved = {}

    def save(name):
        saved[name] = getattr(o, name)

    for name in (
        "load_config",
        "consume_actions",
        "load_state",
        "get_wanted",
        "parse_exclusions",
        "get_episode",
        "probe_audio",
        "choose_source",
        "detect_ladder_source",
        "asr_cache_get",
        "extract_wav",
        "asr_cues",
        "asr_cache_put",
        "process_after_asr",
        "run_upgrades",
        "log",
        "notify_hermes",
        "halt_on_error",
        "append_state",
        "target_sidecar_exists",
        "STATE_FILE",
        "REGISTRY_FILE",
    ):
        save(name)
    o.load_config = lambda: cfg
    o.consume_actions = lambda cfg_: set()
    o.load_state = lambda: list(state or [])
    o.get_wanted = lambda cfg_: mocks["get_wanted_calls"].append(1) or {
        "total": len(wanted or []),
        "data": wanted or [],
    }
    o.parse_exclusions = lambda: set()
    o.get_episode = lambda cfg_, ep_id: infos[ep_id]
    o.probe_audio = lambda path: mocks["probe"].append(path) or [
        {
            "index": 1,
            "codec_name": "aac",
            "tags": {"language": "jpn"},
            "channels": 2,
            "duration": "100.0",
        }
    ]
    o.choose_source = lambda streams, lang: {
        "stream_index": 1,
        "asr_lang": "ja",
        "needs_translate": True,
        "src_lang": "jpn",
    }
    o.detect_ladder_source = lambda *a, **k: {"kind": "asr", "source_path": None}
    o.asr_cache_get = lambda *a: None
    o.extract_wav = lambda path, idx, out: open(out, "w").close()
    o.asr_cues = lambda cfg_, wav, lang: [
        {"start": 0, "end": 1000, "text": "Halo dunia."}
    ]
    o.asr_cache_put = lambda *a: None
    o.process_after_asr = lambda *a, **k: mocks["submit"].append(a) or "done"
    o.run_upgrades = lambda *a, **k: {"upgraded": 0, "checked": 0}
    o.log = lambda msg: mocks["log"].append(msg)
    o.notify_hermes = lambda *a, **k: None
    o.halt_on_error = lambda *a, **k: None
    o.append_state = lambda entry: mocks["state_writes"].append(entry)
    o.target_sidecar_exists = (
        lambda path: mocks["sidecar_calls"].append(path) or sidecar
    )
    o.STATE_FILE = os.path.join(tmp_dir, "state.jsonl")
    o.REGISTRY_FILE = os.path.join(tmp_dir, "subtitle_registry.jsonl")
    with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
        for row in registry_rows or []:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    try:
        stats = o.run_pass()
    finally:
        for name, val in saved.items():
            setattr(o, name, val)
    return stats, mocks


def _mk_regen_info(d, name, ep_id):
    p = os.path.join(d, name)
    open(p, "w").close()
    return {
        "hasFile": True,
        "seasonNumber": 1,
        "episodeNumber": ep_id,
        "seriesId": 99,
        "seriesTitle": "TestShow",
        "episodeFile": {"path": p},
    }


def test_regen_flag_parsing():
    """REGEN_LIBRARY plumbing: 'true'/'1'/'yes' in pipeline.env or
    config.overrides.json (or the process env) enable library regeneration;
    absent/falsey values leave it off; process env beats overrides beats
    env file."""
    import tempfile

    d = tempfile.mkdtemp()
    env_file = os.path.join(d, "pipeline.env")
    ov_file = os.path.join(d, "config.overrides.json")
    saved_env, saved_ov = o.ENV_FILE, o.OVERRIDE_FILE
    saved_var = os.environ.get("REGEN_LIBRARY")
    os.environ.pop("REGEN_LIBRARY", None)
    o.ENV_FILE, o.OVERRIDE_FILE = env_file, ov_file
    try:
        with open(env_file, "w", encoding="utf-8") as fh:
            fh.write("TARGET_LANGS=id\n")
        with open(ov_file, "w", encoding="utf-8") as fh:
            fh.write("{}")
        assert o.load_config()["REGEN_LIBRARY"] is False, "absent -> off"
        for truthy in ("true", "1", "yes", "True", "YES"):
            with open(env_file, "w", encoding="utf-8") as fh:
                fh.write(f"TARGET_LANGS=id\nREGEN_LIBRARY={truthy}\n")
            assert o.load_config()["REGEN_LIBRARY"] is True, truthy
        with open(env_file, "w", encoding="utf-8") as fh:
            fh.write("TARGET_LANGS=id\nREGEN_LIBRARY=true\n")
        with open(ov_file, "w", encoding="utf-8") as fh:
            json.dump({"REGEN_LIBRARY": "0"}, fh)
        assert o.load_config()["REGEN_LIBRARY"] is False, "overrides win"
        os.environ["REGEN_LIBRARY"] = "yes"
        assert o.load_config()["REGEN_LIBRARY"] is True, "env wins"
        os.environ["REGEN_LIBRARY"] = "1"
        assert o.load_config()["REGEN_LIBRARY"] is True
        os.environ["REGEN_LIBRARY"] = "false"
        assert o.load_config()["REGEN_LIBRARY"] is False
    finally:
        o.ENV_FILE, o.OVERRIDE_FILE = saved_env, saved_ov
        if saved_var is None:
            os.environ.pop("REGEN_LIBRARY", None)
        else:
            os.environ["REGEN_LIBRARY"] = saved_var
    print("PASS regen_flag_parsing")


def test_regen_off_no_candidates():
    """REGEN_LIBRARY off: state-done pairs add nothing — only the wanted
    episode is processed, and no regen logs appear."""
    import tempfile

    d = tempfile.mkdtemp()
    infos = {
        101: _mk_regen_info(d, "Ep101.mkv", 1),
        202: _mk_regen_info(d, "Ep202.mkv", 2),
    }
    wanted = [
        {
            "sonarrEpisodeId": 101,
            "seriesTitle": "TestShow",
            "missing_subtitles": [{"code2": "id"}],
        }
    ]
    state = [{"sonarrEpisodeId": 202, "language": "id", "status": "done"}]
    stats, mocks = _run_pass_regen(d, infos, wanted=wanted, state=state, regen=False)
    assert [a[2] for a in mocks["submit"]] == [101], mocks["submit"]
    assert stats["processed"] == 1 and stats["done"] == 1, stats
    assert not any(line.startswith("regen: ") for line in mocks["log"]), mocks["log"]
    print("PASS regen_off_no_candidates")


def test_regen_candidates_from_state():
    """REGEN_LIBRARY on: the latest state-done (ep, lang) pair per key becomes
    a candidate (only TARGET_LANGS, only status=done, latest row wins);
    wanted candidates are unaffected; the pass summary counts regen items."""
    import tempfile

    d = tempfile.mkdtemp()
    infos = {
        101: _mk_regen_info(d, "Ep101.mkv", 1),
        202: _mk_regen_info(d, "Ep202.mkv", 2),
        303: _mk_regen_info(d, "Ep303.mkv", 3),
        404: _mk_regen_info(d, "Ep404.mkv", 4),
    }
    wanted = [
        {
            "sonarrEpisodeId": 101,
            "seriesTitle": "TestShow",
            "missing_subtitles": [{"code2": "id"}],
        }
    ]
    state = [
        {"sonarrEpisodeId": 101, "language": "id", "status": "done"},
        {"sonarrEpisodeId": 202, "language": "id", "status": "error", "seriesTitle": "TestShow"},  # superseded
        {"sonarrEpisodeId": 202, "language": "id", "status": "done", "seriesTitle": "TestShow"},
        {"sonarrEpisodeId": 202, "language": "en", "status": "done", "seriesTitle": "TestShow"},
        {"sonarrEpisodeId": 303, "language": "id", "status": "error"},
        {"sonarrEpisodeId": 404, "language": "fr", "status": "done"},
    ]
    stats, mocks = _run_pass_regen(
        d, infos, wanted=wanted, state=state, regen=True, target_langs=["id", "en"]
    )
    assert stats["done"] == 3 and stats["failed"] == 0, stats
    submits = sorted((a[2], a[3]) for a in mocks["submit"])
    assert submits == [(101, "id"), (202, "en"), (202, "id")], submits
    assert len(mocks["probe"]) == 3
    assert any(
        "regen candidates=2" in line for line in mocks["log"]
    ), mocks["log"]
    assert any(
        "regen: process S01E02 TestShow [id]" in line for line in mocks["log"]
    ), mocks["log"]
    assert any(
        "regen: process S01E02 TestShow [en]" in line for line in mocks["log"]
    ), mocks["log"]
    assert not any(
        "regen: process S01E01" in line for line in mocks["log"]
    ), "wanted item must not be logged as regen"
    print("PASS regen_candidates_from_state")


def test_regen_cap_wanted_priority():
    """MAX_EPS_PER_RUN caps the combined list with wanted candidates first:
    regen pairs only fill leftover slots."""
    import tempfile

    d = tempfile.mkdtemp()
    infos = {
        ep: _mk_regen_info(d, f"Ep{ep}.mkv", ep) for ep in (101, 102, 103, 201, 202)
    }
    wanted = [
        {
            "sonarrEpisodeId": ep,
            "seriesTitle": "TestShow",
            "missing_subtitles": [{"code2": "id"}],
        }
        for ep in (101, 102, 103)
    ]
    state = [
        {"sonarrEpisodeId": 201, "language": "id", "status": "done"},
        {"sonarrEpisodeId": 202, "language": "id", "status": "done"},
    ]
    stats, mocks = _run_pass_regen(
        d, infos, wanted=wanted, state=state, regen=True, max_eps=4
    )
    assert [a[2] for a in mocks["submit"]] == [101, 102, 103, 201], mocks["submit"]
    assert stats["done"] == 4, stats
    stats, mocks = _run_pass_regen(
        d, infos, wanted=wanted, state=state, regen=True, max_eps=2
    )
    assert [a[2] for a in mocks["submit"]] == [101, 102], mocks["submit"]
    assert stats["done"] == 2, stats
    print("PASS regen_cap_wanted_priority")


def test_regen_bypasses_done_and_id_skip():
    """A regen item is processed despite a state-done row AND despite an
    existing target-lang id sidecar (target_sidecar_exists monkeypatched to
    return a path): the ID-skip must not even run for regen items."""
    import tempfile

    d = tempfile.mkdtemp()
    infos = {101: _mk_regen_info(d, "Ep101.mkv", 1)}
    state = [
        {
            "sonarrEpisodeId": 101,
            "language": "id",
            "status": "done",
            "seriesTitle": "TestShow",
        }
    ]
    sidecar = os.path.join(d, "Ep101.id.srt")
    open(sidecar, "w").close()
    stats, mocks = _run_pass_regen(
        d, infos, wanted=[], state=state, regen=True, sidecar=sidecar
    )
    assert stats["done"] == 1 and stats["skipped"] == 0, stats
    assert len(mocks["probe"]) == 1 and len(mocks["submit"]) == 1
    assert mocks["submit"][0][2] == 101 and mocks["submit"][0][3] == "id"
    assert not mocks["sidecar_calls"], "ID-skip must not run for regen items"
    assert not mocks["state_writes"], "no state write in dry run"
    assert any(
        "regen: process S01E01 TestShow [id]" in line for line in mocks["log"]
    ), mocks["log"]
    print("PASS regen_bypasses_done_and_id_skip")


def test_regen_registry_idempotence():
    """A regen item whose (stem, lang) already has a registry row sourced
    asr/jpn/eng is skipped (resumable drain); embedded/external rows describe
    other files and do NOT block."""
    import tempfile

    d = tempfile.mkdtemp()
    infos = {101: _mk_regen_info(d, "Ep101.mkv", 1)}
    state = [{"sonarrEpisodeId": 101, "language": "id", "status": "done"}]
    stem = os.path.splitext(infos[101]["episodeFile"]["path"])[0]
    for src in ("asr", "jpn", "eng"):
        stats, mocks = _run_pass_regen(
            d,
            infos,
            wanted=[],
            state=state,
            regen=True,
            registry_rows=[{"stem": stem, "lang": "id", "source": src}],
        )
        assert stats["done"] == 0 and stats["failed"] == 0, (src, stats)
        assert stats["skipped"] == 1, (src, stats)
        assert not mocks["probe"], (src, "must not probe a registered episode")
        assert not mocks["submit"]
        assert any(
            f"already registered ({src})" in line for line in mocks["log"]
        ), (src, mocks["log"])
    for src in ("embedded", "external"):
        stats, mocks = _run_pass_regen(
            d,
            infos,
            wanted=[],
            state=state,
            regen=True,
            registry_rows=[{"stem": stem, "lang": "id", "source": src}],
        )
        assert stats["done"] == 1 and stats["skipped"] == 0, (src, stats)
        assert len(mocks["probe"]) == 1, (src, "must process despite the row")
        assert len(mocks["submit"]) == 1
        assert not any("already registered" in line for line in mocks["log"])
    print("PASS regen_registry_idempotence")



def test_regen_candidates_skip_registered_before_cap():
    """Pre-cap filter: pairs already registered (source asr/jpn/eng, found by
    episode_id) are NOT added as candidates, so MAX_EPS_PER_RUN slices a
    moving window of unregistered pairs — 10 done pairs, 2 registered
    (ep 1 id+en), cap 8 -> exactly the 8 unregistered ep 2..5 pairs, with no
    in-loop 'already registered' skips."""
    import tempfile

    d = tempfile.mkdtemp()
    infos = {ep: _mk_regen_info(d, f"Ep{ep}.mkv", ep) for ep in range(1, 6)}
    state = [
        {"sonarrEpisodeId": ep, "language": lang, "status": "done"}
        for ep in range(1, 6)
        for lang in ("id", "en")
    ]
    registry_rows = [
        {"stem": os.path.join(d, "Ep1"), "lang": "id", "episode_id": 1, "source": "asr"},
        {"stem": os.path.join(d, "Ep1"), "lang": "en", "episode_id": 1, "source": "jpn"},
    ]
    stats, mocks = _run_pass_regen(
        d,
        infos,
        wanted=[],
        state=state,
        regen=True,
        max_eps=8,
        target_langs=["id", "en"],
        registry_rows=registry_rows,
    )
    assert stats["done"] == 8 and stats["skipped"] == 0 and stats["failed"] == 0, stats
    submits = sorted((a[2], a[3]) for a in mocks["submit"])
    expected = sorted((ep, lang) for ep in range(2, 6) for lang in ("id", "en"))
    assert submits == expected, submits
    assert len(submits) == 8 and all(ep > 1 for ep, _ in submits), submits
    assert any("regen candidates=8" in line for line in mocks["log"]), mocks["log"]
    assert not any("already registered" in line for line in mocks["log"]), (
        "pre-cap filter must remove registered pairs before they reach the loop"
    )
    print("PASS regen_candidates_skip_registered_before_cap")


def test_regen_candidates_include_unregistered_after_registered():
    """Cap headroom does not resurrect registered pairs: 6 done pairs,
    2 registered (ep 1 id+en), cap 8 -> candidates are exactly the 4
    unregistered ep 2..3 pairs."""
    import tempfile

    d = tempfile.mkdtemp()
    infos = {ep: _mk_regen_info(d, f"Ep{ep}.mkv", ep) for ep in range(1, 4)}
    state = [
        {"sonarrEpisodeId": ep, "language": lang, "status": "done"}
        for ep in range(1, 4)
        for lang in ("id", "en")
    ]
    registry_rows = [
        {"stem": os.path.join(d, "Ep1"), "lang": "id", "episode_id": 1, "source": "asr"},
        {"stem": os.path.join(d, "Ep1"), "lang": "en", "episode_id": 1, "source": "jpn"},
    ]
    stats, mocks = _run_pass_regen(
        d,
        infos,
        wanted=[],
        state=state,
        regen=True,
        max_eps=8,
        target_langs=["id", "en"],
        registry_rows=registry_rows,
    )
    assert stats["done"] == 4 and stats["skipped"] == 0 and stats["failed"] == 0, stats
    submits = sorted((a[2], a[3]) for a in mocks["submit"])
    assert submits == sorted((ep, lang) for ep in (2, 3) for lang in ("id", "en")), submits
    assert all(ep > 1 for ep, _ in submits), submits
    assert any("regen candidates=4" in line for line in mocks["log"]), mocks["log"]
    print("PASS regen_candidates_include_unregistered_after_registered")


def test_registry_by_episode_returns_row():
    """registry_by_episode(ep_id, lang): finds the latest row with a matching
    episode_id + lang; rows with null episode_id never match; missing pair or
    lang mismatch -> None."""
    import tempfile

    d = tempfile.mkdtemp()
    saved = o.REGISTRY_FILE
    o.REGISTRY_FILE = os.path.join(d, "subtitle_registry.jsonl")
    try:
        rows = [
            {"stem": "/tv/Ep1", "lang": "id", "episode_id": 1, "source": "asr"},
            {"stem": "/tv/Ep1", "lang": "en", "episode_id": 1, "source": "jpn"},
            {"stem": "/tv/Ep2", "lang": "id", "episode_id": 2, "source": "eng"},
            {"stem": "/tv/EpX", "lang": "id", "episode_id": None, "source": "embedded"},
        ]
        with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        assert o.registry_by_episode(1, "id")["source"] == "asr"
        assert o.registry_by_episode(1, "en")["source"] == "jpn"
        assert o.registry_by_episode(2, "id")["source"] == "eng"
        assert o.registry_by_episode(99, "id") is None, "missing pair -> None"
        assert o.registry_by_episode(1, "fr") is None, "lang mismatch -> None"
        assert o.registry_by_episode(None, "id") is None, "null-episode_id row must not match"
        with open(o.REGISTRY_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"stem": "/tv/Ep1", "lang": "id", "episode_id": 1, "source": "asr2"}) + "\n")
        assert o.registry_by_episode(1, "id")["source"] == "asr2", "latest row wins"
    finally:
        o.REGISTRY_FILE = saved
    print("PASS registry_by_episode_returns_row")



def test_ladder_rung_finds_ja_hi_sidecar():
    """Rung (a) sees Jimaku/Bazarr HI variants: with only {stem}.ja.hi.srt
    (or {stem}.jpn.hi.srt) on disk, it is found and used as the jpn source —
    no bazarr query, no ASR."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    saved_bazarr = o.bazarr_jpn_candidate
    calls = []
    o.bazarr_jpn_candidate = lambda *a, **k: calls.append(a) or None
    try:
        cfg = build_cfg()
        cfg["TMP_DIR"] = d
        cfg["ALIGN_ENABLED"] = "false"
        for name in ("Ep.ja.hi.srt", "Ep.jpn.hi.srt"):
            cand = os.path.join(d, name)
            _write_srt(cand, [(i * 1000, i * 1000 + 800, "テストの台詞です。あいうえおかきくけこさしすせそ。") for i in range(60)])
            v = o.detect_ladder_source(cfg, mkv, "id", d, ep_id=1, series_id=2)
            assert v["kind"] == "jpn" and v["source_path"] == cand, (name, v)
            os.remove(cand)
        assert not calls, "bazarr must not be queried when the HI sidecar is on disk"
    finally:
        o.bazarr_jpn_candidate = saved_bazarr
    print("PASS ladder_rung_finds_ja_hi_sidecar")


def test_bazarr_jpn_positive_cache():
    """bazarr_jpn_candidate caches a FOUND path positively: the 2nd call for
    the same (ep_id, series_id) returns the same path with no re-query; a
    miss stays one-shot per key."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    jpn = os.path.join(d, "Ep.jpn.srt")
    _write_srt(jpn, [(i * 1000, i * 1000 + 800, "テストの台詞です。あいうえお") for i in range(3)])
    saved_requests = o.requests
    saved_tried = o._BAZARR_JPN_TRIED
    saved_cache = o._BAZARR_JPN_CACHE
    o._BAZARR_JPN_TRIED = set()
    o._BAZARR_JPN_CACHE = {}
    hits = []

    class FakeResp:
        def __init__(self, code, payload):
            self.status_code, self._payload = code, payload

        def json(self):
            return self._payload

    class FakeReq:
        def __init__(self, payload):
            self._payload = payload

        def get(self, url, params=None, headers=None, timeout=None):
            hits.append(("get", url))
            return FakeResp(200, self._payload)

        def post(self, url, json=None, headers=None, timeout=None):
            hits.append(("post", url))
            return FakeResp(204, None)

    cfg = {"BAZARR_URL": "http://x/api", "BAZARR_API_KEY": "k"}
    try:
        o.requests = FakeReq({"data": [{"subtitles": [{"code2": "ja", "path": mkv}]}]})
        p1 = o.bazarr_jpn_candidate(cfg, 1, 2, mkv, d)
        p2 = o.bazarr_jpn_candidate(cfg, 1, 2, mkv, d)
        assert p1 == mkv and p2 == mkv, (p1, p2)
        assert len(hits) == 1, "2nd call must not re-query Bazarr"
        # miss: query once, then tried forever
        os.remove(jpn)  # the download poll must not find the hit-case file
        o._BAZARR_JPN_TRIED = set()
        o._BAZARR_JPN_CACHE = {}
        hits.clear()
        o.requests = FakeReq({"data": [{"subtitles": []}]})
        assert o.bazarr_jpn_candidate(cfg, 3, 4, mkv, d) is None
        assert o.bazarr_jpn_candidate(cfg, 3, 4, mkv, d) is None
        assert len(hits) == 2, "miss is one-shot (get+post on first call only)"
    finally:
        o.requests = saved_requests
        o._BAZARR_JPN_TRIED = saved_tried
        o._BAZARR_JPN_CACHE = saved_cache
    print("PASS bazarr_jpn_positive_cache")


def test_webhook_dedup_registers_existing_sidecars():
    """Pre-existing extract without a registry row: when the embedded stream
    is gone (post-remux) the webhook can no longer re-extract, but the
    on-disk sidecar still gains its embedded row (audio_id from the current
    video); an identical embedded row is not re-appended (idempotent)."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    stem = os.path.splitext(mkv)[0]
    jpn = os.path.join(d, "Ep.jpn.srt")
    _write_srt(jpn, [(i * 1000, i * 1000 + 800, "テストの台詞です。あいうえお") for i in range(3)])
    fake_streams = o._AudioStreams(
        [{"index": 1, "codec_name": "aac", "tags": {"language": "jpn"},
          "channel_layout": "stereo", "duration": "60.0"}]
    )
    fake_streams.format_duration = 60.0
    saved = {name: getattr(o, name) for name in
             ("REGISTRY_FILE", "probe_audio", "_subtitle_streams")}
    saved_run = o.subprocess.run
    o.REGISTRY_FILE = os.path.join(d, "registry.jsonl")
    open(o.REGISTRY_FILE, "w", encoding="utf-8").close()
    o.probe_audio = lambda path: fake_streams
    o._subtitle_streams = lambda path: []  # streams stripped by the remux
    o.subprocess.run = lambda cmd, **kw: (_ for _ in ()).throw(
        AssertionError(f"no subprocess expected, got {cmd[0]}")
    )
    try:
        o.extract_subtitle_sidecars(mkv, tdarr_id="t1")
        row = o.registry_get(stem, "jpn")
        assert row is not None and row["source_kind"] == "embedded", row
        assert row["source_hash"] == o.file_sha256(jpn), row
        assert row["audio_id"] == o.audio_stream_signature(fake_streams), row
        assert o.registry_get(stem, "eng") is None, "no eng sidecar -> no row"
        n_before = sum(1 for _ in open(o.REGISTRY_FILE))
        o.extract_subtitle_sidecars(mkv, tdarr_id="t2")
        n_after = sum(1 for _ in open(o.REGISTRY_FILE))
        assert n_after == n_before, (n_before, n_after)
    finally:
        o.subprocess.run = saved_run
        for name, val in saved.items():
            setattr(o, name, val)
    print("PASS webhook_dedup_registers_existing_sidecars")


def test_ladder_adopts_untrusted_webhook_extract():
    """Adoption: an untrusted {stem}.jpn.srt (no registry row) whose content
    hash matches a fresh embedded extract is registered source_kind
    'embedded' and used WITHOUT the re-timing gate; a hash mismatch leaves it
    untrusted (the retime gate rejects: no anchors, falls through); .ja.srt
    Jimaku files are never adopted."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    stem = os.path.splitext(mkv)[0]
    jpn = os.path.join(d, "Ep.jpn.srt")
    _write_srt(jpn, [(i * 1000, i * 1000 + 800, "テストの台詞です。あいうえおかきくけこさしすせそ。") for i in range(60)])
    fake_streams = o._AudioStreams(
        [{"index": 1, "codec_name": "aac", "tags": {"language": "jpn"},
          "channel_layout": "stereo", "duration": "60.0"}]
    )
    fake_streams.format_duration = 60.0
    saved = {name: getattr(o, name) for name in
             ("REGISTRY_FILE", "probe_audio", "extract_embedded_subtitle",
              "bazarr_jpn_candidate", "_ADOPT_TRIED", "_retime_anchor_asr")}
    saved_run = o.subprocess.run
    o._retime_anchor_asr = lambda *a, **k: ([], "ja")
    o.REGISTRY_FILE = os.path.join(d, "registry.jsonl")
    o.probe_audio = lambda path: fake_streams
    o.bazarr_jpn_candidate = lambda *a, **k: None
    calls = []
    o.subprocess.run = lambda cmd, **kw: calls.append(cmd[0]) or _run_result(1)
    extract_src = [jpn]

    def fake_extract(path, lang, out):
        data = open(extract_src[0], "rb").read()
        with open(out, "wb") as fh:
            fh.write(data)
        return True
    o.extract_embedded_subtitle = fake_extract
    cfg = build_cfg()
    cfg["TMP_DIR"] = d
    cfg["ALIGN_ENABLED"] = "true"
    try:
        # scenario 1: hash matches -> adoption, no alignment, hit returned
        open(o.REGISTRY_FILE, "w", encoding="utf-8").close()
        o._ADOPT_TRIED.clear()
        calls.clear()
        v = o.detect_ladder_source(cfg, mkv, "id", d, ep_id=1, series_id=2)
        assert v["kind"] == "jpn" and v["source_path"] == jpn, v
        assert v.get("align_tmp") is None, v
        assert "ffs" not in calls and "ffmpeg" not in calls, calls
        row = o.registry_get(stem, "jpn")
        assert row and row["source_kind"] == "embedded", row
        assert row["source_hash"] == o.file_sha256(jpn), row
        # scenario 2: hash mismatch -> no adoption, alignment runs, falls to asr
        open(o.REGISTRY_FILE, "w", encoding="utf-8").close()
        o._ADOPT_TRIED.clear()
        calls.clear()
        bad = os.path.join(d, "bad_extract.srt")
        _write_srt(bad, [(i * 1000, i * 1000 + 800, "テスト") for i in range(3)])
        extract_src[0] = bad
        v = o.detect_ladder_source(cfg, mkv, "id", d, ep_id=1, series_id=2)
        assert v["kind"] == "asr", v
        assert "ffs" not in calls, calls
        assert o.registry_get(stem, "jpn") is None, "no row after a mismatch"
        # scenario 3: .ja.srt (Jimaku) is never adopted
        open(o.REGISTRY_FILE, "w", encoding="utf-8").close()
        o._ADOPT_TRIED.clear()
        calls.clear()
        ja = os.path.join(d, "Ep.ja.srt")
        _write_srt(ja, [(i * 1000, i * 1000 + 800, "テストの台詞です。あいうえおかきくけこさしすせそ。") for i in range(60)])
        v = o.detect_ladder_source(cfg, mkv, "id", d, ep_id=1, series_id=2)
        assert v["kind"] == "asr", v
        assert "ffs" not in calls, calls
        assert o.registry_get(stem, "jpn") is None, "Jimaku files are never adopted"
    finally:
        o.subprocess.run = saved_run
        for name, val in saved.items():
            setattr(o, name, val)
    print("PASS ladder_adopts_untrusted_webhook_extract")


def test_assess_source_file_cleans_ass_styling():
    """A styling-heavy ASS-derived SRT ({\\an8} blocks, <font> tags, \\N
    breaks) still passes the jpn gates: clean_ass_text runs per cue BEFORE
    the cue/char/CJK counts (verified: the E05 embedded extract gate rejects
    came from the stream being gone, not styling junk)."""
    import tempfile

    d = tempfile.mkdtemp()
    srt = os.path.join(d, "styled.srt")
    cues = []
    for i in range(60):
        cues.append((i * 1000, i * 1000 + 800,
                     f'<font size="75">{{\\an8}}(フリーレン)\\N王都が見えてきたね テストの台詞です{i}。{"あいうえお" * 4}</font>'))
    _write_srt(srt, cues)
    v = o.assess_source_file(build_cfg(), srt, "jpn", 60.0)
    assert v["ok"], v
    assert len(v["cues"]) == 60, len(v["cues"])
    assert "\\an8" not in v["cues"][0]["text"], v["cues"][0]["text"]
    assert "font" not in v["cues"][0]["text"], v["cues"][0]["text"]
    assert "王都が見えてきたね" in v["cues"][0]["text"], v["cues"][0]["text"]
    print("PASS assess_source_file_cleans_ass_styling")


def test_jellyfin_refresh_fallback_terms():
    """When the title SearchTerm misses (Jellyfin Name punctuation differs
    from the Sonarr title), the refresh falls back to the title prefix
    before the separator and then the filename stem; the path match still
    gates the refresh POST."""
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
            calls.append(("GET", dict(params or {})))
            if (params or {}).get("SearchTerm") == "Ep Title: Subtitle":
                return FakeResp({"Items": []})
            return FakeResp({"Items": [
                {"Id": "abc123",
                 "Path": "/media/jellyfin/sonarr-tv-shows/X/Ep.mkv"}
            ]})

        @staticmethod
        def post(url, json=None, headers=None, timeout=None):
            calls.append(("POST", url))
            return FakeResp(None, 204)

    saved_requests = o.requests
    o.requests = FakeRequests
    try:
        cfg = {"JELLYFIN_API_KEY": "k123", "JELLYFIN_URL": "http://jf:8096"}
        o.jellyfin_refresh(
            cfg,
            "/mnt/nas/share/media/jellyfin/sonarr-tv-shows/X/Ep.mkv",
            "Ep Title: Subtitle",
        )
        deadline = time.time() + 5
        while len(calls) < 3 and time.time() < deadline:
            time.sleep(0.05)
        assert len(calls) == 3, calls
        terms = [c[1]["SearchTerm"] for c in calls if c[0] == "GET"]
        assert terms == ["Ep Title: Subtitle", "Ep Title"], terms
        assert calls[-1][0] == "POST" and calls[-1][1].endswith("/Items/abc123/Refresh")
    finally:
        o.requests = saved_requests
    print("PASS jellyfin_refresh_fallback_terms")




# ---------- ASR-anchored re-timing (retime gate) ----------


def test_retime_global_offset_text_anchors():
    """Global +8s offset with matching text: every cue text-anchors to its
    ASR cue; the first retimed cue start equals the ASR first start."""
    asr = [{"start": 9000.0 + i * 4000, "end": 11500.0 + i * 4000,
            "text": f"テストの台詞です。{i} はい。"} for i in range(40)]
    sub = [{"start": c["start"] + 8000, "end": c["end"] + 8000, "text": c["text"]}
           for c in asr]
    out, stats = o.retime_external_cues(sub, asr, "jpn", 600.0)
    assert out is not None
    assert stats["method"] == "text" and stats["anchors"] == 40, stats
    assert stats["matched_frac"] == 1.0, stats
    assert out[0]["start"] == asr[0]["start"], (out[0], asr[0])
    for i in range(40):
        assert abs(out[i]["start"] - asr[i]["start"]) < 1e-6, (i, out[i])
    print("PASS retime_global_offset_text_anchors")


def test_retime_jaadugar_pattern():
    """Jaadugar S01E04 pattern: cue 0 off by -8s (1.126s vs speech at 9.07s),
    one SDH music cue, cues 2+ aligned ±0.2s — cue 0 lands on ASR cue 0,
    later cues keep their timing, the SDH cue survives."""
    asr = [{"start": 9070.0 + i * 4000, "end": 9070.0 + i * 4000 + 2500,
            "text": f"セリフの内容です。{i}"} for i in range(60)]
    sub = [{"start": 1126.0, "end": 3626.0, "text": asr[0]["text"]}]
    sub.append({"start": 8133.0, "end": 9033.0, "text": "♬ 音楽 ♬"})
    for i in range(2, 60):
        s = asr[i]["start"] + (-200 if i % 3 == 0 else 120)
        sub.append({"start": s, "end": s + 2500, "text": asr[i]["text"]})
    out, stats = o.retime_external_cues(sub, asr, "jpn", 1435.42)
    assert out is not None
    assert len(out) == 60, "SDH cue must not be dropped"
    assert out[0]["start"] == asr[0]["start"], (out[0], asr[0])
    assert "♬" in out[1]["text"]
    for i in range(2, 60):
        assert abs(out[i]["start"] - asr[i]["start"]) < 0.2 * 1000, (i, out[i], asr[i])
    assert stats["method"] == "mixed", stats
    print("PASS retime_jaadugar_pattern")


def test_retime_order_fallback_garbage_asr():
    """Ghost Stories E15 pattern: ASR text is English garbage, timestamps
    accurate — no text anchors, order-preserving mode: monotonic starts,
    first cue within 2s of the ASR first start."""
    asr = [{"start": 5000.0 + i * 3000, "end": 5000.0 + i * 3000 + 2000,
            "text": f"Queen Latifah dance break {i}"} for i in range(50)]
    sub = [{"start": i * 3000.0, "end": i * 3000.0 + 2400,
            "text": f"日本語の台詞です。{i}"} for i in range(50)]
    out, stats = o.retime_external_cues(sub, asr, "jpn", 300.0)
    assert out is not None and stats["method"] == "order", stats
    assert stats["anchors"] == 0, stats
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), starts
    assert abs(out[0]["start"] - asr[0]["start"]) < 2000.0, (out[0], asr[0])
    print("PASS retime_order_fallback_garbage_asr")


def test_retime_count_mismatch_284_346():
    """Count mismatch (284 sub cues vs 346 ASR segments): order-preserving
    mapping, every cue mapped, monotonic, no overlaps, last end within the
    video duration."""
    asr = [{"start": 1000.0 + i * 4000, "end": 1000.0 + i * 4000 + 3000,
            "text": f"スピーチ {i}"} for i in range(346)]
    sub = [{"start": 2000.0 + i * 5000, "end": 2000.0 + i * 5000 + 4000,
            "text": f"字幕の台詞です。{i} です。"} for i in range(284)]
    out, stats = o.retime_external_cues(sub, asr, "jpn", 1400.0)
    assert out is not None and len(out) == 284, stats
    assert stats["method"] == "order", stats
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), "monotonic"
    for i in range(283):
        assert out[i]["end"] <= out[i + 1]["start"] + 1e-9, (i, out[i], out[i + 1])
    assert out[-1]["end"] <= 1400.0 * 1000, out[-1]
    print("PASS retime_count_mismatch_284_346")


def test_retime_ratio_reject():
    """100 sub cues vs 5 ASR segments: order ratio 0.05 < 1/3 -> REJECT
    (None) — release mismatch, same fall-through semantics as the old gate."""
    asr = [{"start": 1000.0 + i * 20000, "end": 5000.0 + i * 20000,
            "text": f"スピーチ {i}"} for i in range(5)]
    sub = [{"start": i * 1000.0, "end": i * 1000.0 + 700,
            "text": f"字幕の台詞です。{i}"} for i in range(100)]
    out, stats = o.retime_external_cues(sub, asr, "jpn", 200.0)
    assert out is None, out
    assert stats["method"] == "order", stats
    print("PASS retime_ratio_reject")


def test_retime_sdh_never_dropped():
    """SDH/music cues (♬, （音）, speaker-only) in the sub: interpolated like
    unanchored cues, never negative, never dropped."""
    asr = [{"start": 5000.0 + i * 4000, "end": 5000.0 + i * 4000 + 2500,
            "text": f"セリフの内容です。{i}"} for i in range(20)]
    sub = []
    for i in range(20):
        if i % 4 == 1:
            sub.append({"start": i * 4000.0, "end": i * 4000.0 + 3000, "text": "♬ ♬"})
        elif i % 4 == 2:
            sub.append({"start": i * 4000.0, "end": i * 4000.0 + 3000, "text": "（音）"})
        elif i % 4 == 3:
            sub.append({"start": i * 4000.0, "end": i * 4000.0 + 3000, "text": "（誰か）"})
        else:
            sub.append({"start": i * 4000.0 - 3000, "end": i * 4000.0 + 1000,
                        "text": asr[i]["text"]})
    out, stats = o.retime_external_cues(sub, asr, "jpn", 200.0)
    assert out is not None and len(out) == 20, "SDH cues must never be dropped"
    assert all(c["start"] >= 0.0 for c in out), out
    assert "♬" in out[1]["text"] and "（音）" in out[2]["text"] and "（誰か）" in out[3]["text"]
    assert stats["method"] == "mixed", stats
    print("PASS retime_sdh_never_dropped")


def test_retime_end_clamp():
    """Ends clamped to next cue start - 0.05s; the last cue end clamped to
    the video duration; starts never negative. (The last anchor sits before
    the duration clamp point so the clamped last cue keeps a real duration
    >= 0.05s and passes the degenerate guard.)"""
    asr = [
        {"start": 10000.0, "end": 15000.0, "text": "セリフの内容です。0"},
        {"start": 30000.0, "end": 35000.0, "text": "セリフの内容です。1"},
        {"start": 45000.0, "end": 50000.0, "text": "セリフの内容です。2"},
    ]
    sub = [
        {"start": 0.0, "end": 30000.0, "text": asr[0]["text"]},
        {"start": 5000.0, "end": 25000.0, "text": "はやい"},
        {"start": 20000.0, "end": 60000.0, "text": asr[2]["text"]},
    ]
    out, stats = o.retime_external_cues(sub, asr, "jpn", 50.0)
    assert out is not None and stats["method"] == "mixed", stats
    for i in range(2):
        assert out[i]["end"] <= out[i + 1]["start"] - 49.999, (i, out[i], out[i + 1])
    assert out[-1]["end"] <= 50.0 * 1000, out[-1]
    assert all(c["start"] >= 0.0 for c in out), out
    assert out[0]["end"] - out[0]["start"] <= 20000.0 - 50.0 + 1e-6, out[0]
    assert all(c["end"] - c["start"] >= 50.0 for c in out), out
    print("PASS retime_end_clamp")


def test_retime_eng_sub_order():
    """eng sub vs ja ASR: text cannot match (no kana overlap) — the sub never
    text-anchors; order-preserving mapping, monotonic."""
    asr = [{"start": 5000.0 + i * 3000, "end": 5000.0 + i * 3000 + 2000,
            "text": f"日本語の音声です。{i}"} for i in range(30)]
    sub = [{"start": i * 3000.0, "end": i * 3000.0 + 2500,
            "text": f"This is english subtitle line {i}."} for i in range(30)]
    out, stats = o.retime_external_cues(sub, asr, "eng", 200.0)
    assert out is not None and stats["method"] == "order", stats
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), starts
    assert out[0]["start"] == asr[0]["start"], out[0]
    print("PASS retime_eng_sub_order")


def test_retime_anchor_asr_cache():
    """_retime_anchor_asr: cache HIT skips wav extraction and ASR and does
    not re-put; a MISS extracts, transcribes and puts to the cache; a probe
    failure returns None (never raises)."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    fake_streams = o._AudioStreams(
        [{"index": 1, "codec_name": "aac", "tags": {"language": "jpn"},
          "channel_layout": "stereo", "duration": "60.0"}]
    )
    fake_streams.format_duration = 60.0
    cues = [{"start": 1000.0, "end": 2000.0, "text": "はい"}]
    saved = {}
    for n in ("probe_audio", "asr_cache_get", "extract_wav", "asr_cues", "asr_cache_put"):
        saved[n] = getattr(o, n)
    calls = {"extract": 0, "asr": 0, "put": 0}
    o.probe_audio = lambda path: fake_streams
    o.asr_cache_get = lambda *a: None
    o.extract_wav = lambda *a, **k: calls.__setitem__("extract", calls["extract"] + 1)
    o.asr_cues = lambda *a, **k: calls.__setitem__("asr", calls["asr"] + 1) or cues
    o.asr_cache_put = lambda *a, **k: calls.__setitem__("put", calls["put"] + 1)
    cfg = build_cfg()
    cfg["TMP_DIR"] = d
    try:
        got, asr_lang = o._retime_anchor_asr(cfg, mkv, "jpn")
        assert got == cues and asr_lang == "ja", (got, asr_lang)
        assert calls == {"extract": 1, "asr": 1, "put": 1}, calls
        leftover = [f for f in os.listdir(d) if f.startswith("retime_anchor_")]
        assert not leftover, leftover
        calls["extract"] = calls["asr"] = calls["put"] = 0
        o.asr_cache_get = lambda *a: cues  # hit next time
        got, asr_lang = o._retime_anchor_asr(cfg, mkv, "jpn")
        assert got == cues and asr_lang == "ja", (got, asr_lang)
        assert calls == {"extract": 0, "asr": 0, "put": 0},             "cache hit must skip extract/asr/put"
        o.probe_audio = lambda path: (_ for _ in ()).throw(RuntimeError("boom"))
        assert o._retime_anchor_asr(cfg, mkv, "jpn") == (None, None)
    finally:
        for n, v in saved.items():
            setattr(o, n, v)
    print("PASS retime_anchor_asr_cache")


def test_retime_disabled_external_hit_unchanged():
    """lc retime_enabled false: the external sidecar is returned unchanged —
    no retime gate, no ASR anchor lookups, no align_tmp."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    jpn = os.path.join(d, "Ep.jpn.srt")
    _write_srt(jpn, [(i * 1000, i * 1000 + 800, "テストの台詞です。あいうえおかきくけこさしすせそ。") for i in range(60)])
    saved = {}
    for n in ("REGISTRY_FILE", "extract_embedded_subtitle", "bazarr_jpn_candidate",
              "_retime_anchor_asr", "_ADOPT_TRIED"):
        saved[n] = getattr(o, n)
    o.REGISTRY_FILE = os.path.join(d, "registry.jsonl")
    open(o.REGISTRY_FILE, "w", encoding="utf-8").close()
    o.extract_embedded_subtitle = lambda *a, **k: False
    o.bazarr_jpn_candidate = lambda *a, **k: None
    o._ADOPT_TRIED = set()
    o._retime_anchor_asr = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("retime must not run when disabled")
    )
    try:
        cfg = build_cfg()
        cfg["TMP_DIR"] = d
        cfg["RETIME_ENABLED"] = "false"
        v = o.detect_ladder_source(cfg, mkv, "id", d, ep_id=1, series_id=2)
        assert v["kind"] == "jpn" and v["source_path"] == jpn, v
        assert v.get("align_tmp") is None and "align_stats" not in v, v
    finally:
        for n, val in saved.items():
            setattr(o, n, val)
    print("PASS retime_disabled_external_hit_unchanged")


def test_retime_subtitle_glue_ass_and_reject():
    """retime_external_subtitle end-to-end: .ass input converts to a temp
    SRT (input untouched) and the retimed SRT lands under tmp_dir; a retime
    REJECT (anchors empty) returns (None, stats) with no output file."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Ep.mkv")
    open(mkv, "w").close()
    ass = os.path.join(d, "Ep.jpn.ass")
    ass_text = (
        "[Script Info]\nTitle: x\n\n"
        "[Events]\n"
        "Dialogue: 0,0:00:01.00,0:00:01.80,Default,,0,0,0,,テストです。\n"
        "Dialogue: 0,0:00:02.00,0:00:02.80,Default,,0,0,0,,はい、そうです。\n"
    )
    with open(ass, "w", encoding="utf-8") as fh:
        fh.write(ass_text)
    anchors = [
        {"start": 9000.0, "end": 10000.0, "text": "テストです。"},
        {"start": 15000.0, "end": 16000.0, "text": "はい、そうです。"},
    ]
    saved = {}
    saved_run = o.subprocess.run
    for n in ("_retime_anchor_asr", "media_duration_s"):
        saved[n] = getattr(o, n)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd[0])
        if cmd[0] == "ffmpeg":
            _write_srt(cmd[-1], [(1000, 1800, "テストです。"), (2000, 2800, "はい、そうです。")])
            return _run_result(0)
        return _run_result(1)
    o.subprocess.run = fake_run
    o._retime_anchor_asr = lambda *a, **k: (anchors, "ja")
    o.media_duration_s = lambda path: 100.0
    out_path = None
    try:
        out_path, stats = o.retime_external_subtitle(ass, mkv, build_cfg(), d, sub_lang="jpn")
        assert out_path and os.path.isfile(out_path), out_path
        assert os.path.dirname(out_path) == d, out_path
        assert "ffmpeg" in calls and "ffs" not in calls, calls
        assert open(ass, encoding="utf-8").read() == ass_text, "input must be untouched"
        with open(out_path, encoding="utf-8") as fh:
            cues = o.parse_srt(fh.read())
        assert len(cues) == 2
        assert abs(o.srt_ts_ms(cues[0]["start"]) - 9000.0) < 1.0, cues[0]
        assert abs(o.srt_ts_ms(cues[1]["start"]) - 15000.0) < 1.0, cues[1]
        assert stats["method"] == "text", stats
        # reject: anchors empty -> (None, stats), no output file
        o._retime_anchor_asr = lambda *a, **k: ([], "ja")
        out2, stats2 = o.retime_external_subtitle(ass, mkv, build_cfg(), d, sub_lang="jpn")
        assert out2 is None and stats2 is not None, (out2, stats2)
        assert stats2["method"] is None and stats2["total"] == 2, stats2
        leftovers = [f for f in os.listdir(d) if f.startswith(("align_", "retime_"))]
        assert leftovers == [os.path.basename(out_path)], leftovers
    finally:
        if out_path:
            os.remove(out_path)
        o.subprocess.run = saved_run
        for n, val in saved.items():
            setattr(o, n, val)
    print("PASS retime_subtitle_glue_ass_and_reject")


def test_retime_ghost_stories_e14_pattern():
    """Ghost Stories S01E14: 344 ja sub cues vs 344 ASR cues whose text is
    English hallucination, plus ONE accidental 1-char match — the 1-char
    text is ignored (< 2 normalized chars) and anchors/total (0/344) <
    RETIME_MIN_ANCHOR_FRAC anyway: pure ORDER mode; the first retimed cue
    lands within 2s of the first speech segment, real durations, monotonic."""
    asr = [{"start": 6700.0 + i * 4000, "end": 6700.0 + i * 4000 + 3000,
            "text": f"I love that! {i}"} for i in range(344)]
    asr[172]["text"] = "あ"  # accidental 1-char candidate: must not anchor
    sub = [{"start": 1000.0 + i * 4100, "end": 1000.0 + i * 4100 + 2800,
            "text": f"日本語の台詞です。{i}"} for i in range(344)]
    sub[172]["text"] = "あ"  # would Jaccard 1.0 vs asr[172] if not skipped
    out, stats = o.retime_external_cues(sub, asr, "jpn", 1400.0)
    assert out is not None and stats["method"] == "order", stats
    assert stats["anchors"] == 0, stats
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), "monotonic"
    assert abs(out[0]["start"] - asr[0]["start"]) < 2000.0, (out[0], asr[0])
    assert all(c["end"] - c["start"] >= 50.0 for c in out), out
    print("PASS retime_ghost_stories_e14_pattern")


def test_retime_sparse_anchors_force_order():
    """3 real text anchors out of 344 cues (matched_frac 0.009 < 0.25): all
    text anchors are DISCARDED — pure order mode, never the extrapolating
    mixed mode that produced the 0.000 -> 0.000 degenerate cues."""
    asr = [{"start": 5000.0 + i * 4000, "end": 5000.0 + i * 4000 + 3000,
            "text": f"スピーチの音声です。{i}"} for i in range(344)]
    sub = [{"start": i * 4100.0, "end": i * 4100.0 + 2800,
            "text": f"日本語の台詞です。{i}"} for i in range(344)]
    for k, j in ((10, 100), (200, 200), (300, 300)):
        sub[j]["text"] = asr[k]["text"]  # three real matches, far too sparse
    out, stats = o.retime_external_cues(sub, asr, "jpn", 1400.0)
    assert out is not None and stats["method"] == "order", stats
    assert stats["anchors"] == 0, stats
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), "monotonic"
    assert abs(out[0]["start"] - asr[0]["start"]) < 2000.0, out[0]
    print("PASS retime_sparse_anchors_force_order")


def test_retime_degenerate_guard_fallback():
    """Belt-and-braces: a mixed build whose interpolation produces an
    INVERTED start (an out-of-order sub cue start extrapolates before the
    previous cue) trips the degenerate guard; the output falls back to pure
    ORDER mapping — all starts >= 0, real durations, monotonic."""
    asr = [{"start": 10000.0 + i * 4000, "end": 10000.0 + i * 4000 + 3000,
            "text": f"スピーチの内容です。{i}"} for i in range(4)]
    sub = [
        {"start": 3000.0, "end": 5500.0, "text": asr[0]["text"]},
        {"start": 0.0, "end": 2500.0, "text": "はやい"},
        {"start": 6000.0, "end": 8500.0, "text": asr[2]["text"]},
        {"start": 9000.0, "end": 11500.0, "text": asr[3]["text"]},
    ]
    out, stats = o.retime_external_cues(sub, asr, "jpn", 200.0)
    assert out is not None and stats["method"] == "order", stats
    assert stats["anchors"] == 0, stats
    assert all(c["start"] >= 0.0 for c in out), out
    assert all(c["end"] - c["start"] >= 50.0 for c in out), out
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), starts
    print("PASS retime_degenerate_guard_fallback")


def test_retime_mixed_threshold_kept():
    """matched_frac 0.5 (>= RETIME_MIN_ANCHOR_FRAC 0.25): the anchor set is
    KEPT and mixed interpolation is used — never discarded to order."""
    asr = [{"start": 5000.0 + i * 4000, "end": 5000.0 + i * 4000 + 3000,
            "text": f"セリフの内容です。{i}"} for i in range(10)]
    sub = [{"start": i * 4000.0, "end": i * 4000.0 + 3000,
            "text": f"字幕の台詞です。{i}"} for i in range(10)]
    for j in range(0, 10, 2):
        sub[j]["text"] = asr[j]["text"]  # 5/10 = 0.5
    out, stats = o.retime_external_cues(sub, asr, "jpn", 200.0)
    assert out is not None and stats["method"] == "mixed", stats
    assert stats["anchors"] == 5 and stats["matched_frac"] == 0.5, stats
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), starts
    print("PASS retime_mixed_threshold_kept")

def test_retime_zero_length_asr_filtered():
    """A whisper-artifact ASR segment with end <= start (zero length) is
    filtered before any mapping; the surviving segments map order-preservingly
    (including a duplicate-segment group), no reject, all durations real."""
    asr = [
        {"start": 5000.0, "end": 8000.0, "text": "スピーチです。0"},
        {"start": 7000.0, "end": 6000.0, "text": "ghost zero-length"},
        {"start": 10000.0, "end": 13000.0, "text": "スピーチです。2"},
    ]
    sub = [{"start": i * 3000.0, "end": i * 3000.0 + 2500,
            "text": f"字幕の台詞です。{i}"} for i in range(3)]
    out, stats = o.retime_external_cues(sub, asr, "jpn", 200.0)
    assert out is not None and stats["method"] == "order", stats
    assert all(c["end"] - c["start"] >= 50.0 for c in out), out
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), starts
    assert abs(out[0]["start"] - 5000.0) < 1e-6, out[0]
    print("PASS retime_zero_length_asr_filtered")


def test_retime_regression_anchors_dropped():
    """False-match regressions: matching yields [(0,5),(1,2),(2,7)] — the
    (1,2) anchor goes BACKWARD (asr 2 < 5) and is dropped; the monotonic
    subset [(0,5),(2,7)] is used, output stays monotonic and mixed."""
    asr = [{"start": 1000.0 + i * 4000, "end": 1000.0 + i * 4000 + 3000,
            "text": f"スピーチです。{i}"} for i in range(8)]
    sub = [
        {"start": 0.0, "end": 2500.0, "text": asr[5]["text"]},
        {"start": 3000.0, "end": 5500.0, "text": asr[2]["text"]},
        {"start": 6000.0, "end": 8500.0, "text": asr[7]["text"]},
    ]
    out, stats = o.retime_external_cues(sub, asr, "jpn", 200.0)
    assert out is not None and stats["method"] == "mixed", stats
    assert stats["anchors"] == 2, stats
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), starts
    assert out[0]["start"] == asr[5]["start"], out[0]
    assert out[2]["start"] == asr[7]["start"], out[2]
    print("PASS retime_regression_anchors_dropped")


def test_retime_duplicate_segment_group():
    """10 sub cues vs 8 ASR segments: consecutive sub cues map to the SAME
    segment — grouped cues share the segment start, each end capped at the
    segment's OWN end (never clamped against an equal start), monotonic,
    no cue shorter than 0.05s."""
    asr = [{"start": 5000.0 + i * 4000, "end": 5000.0 + i * 4000 + 3500,
            "text": f"スピーチです。{i}"} for i in range(8)]
    sub = [{"start": i * 3500.0, "end": i * 3500.0 + 3000,
            "text": f"字幕の台詞です。{i}"} for i in range(10)]
    out, stats = o.retime_external_cues(sub, asr, "jpn", 200.0)
    assert out is not None and stats["method"] == "order", stats
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), starts
    assert all(c["end"] - c["start"] >= 50.0 for c in out), out
    assert all(c["end"] <= c["start"] + 3500.0 + 1e-9 for c in out),         "group cues end within their segment end"
    print("PASS retime_duplicate_segment_group")


def test_retime_e14_zero_length_segment():
    """E14-style: 445 ASR cues with garbage text, ONE zero-length whisper
    artifact among them — the artifact is filtered, order mode maps against
    the remaining 444 real segments, first cue near the first real speech
    segment, no reject, no degenerate cues."""
    asr = [{"start": 6700.0 + i * 4000, "end": 6700.0 + i * 4000 + 3000,
            "text": f"Nice rack. {i}"} for i in range(445)]
    asr[300] = {"start": 10000.0, "end": 10000.0, "text": "ghost"}
    sub = [{"start": 1000.0 + i * 4100, "end": 1000.0 + i * 4100 + 2800,
            "text": f"日本語の台詞です。{i}"} for i in range(445)]
    out, stats = o.retime_external_cues(sub, asr, "jpn", 1800.0)
    assert out is not None and stats["method"] == "order", stats
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), starts
    assert abs(out[0]["start"] - asr[0]["start"]) < 2000.0, out[0]
    assert all(c["end"] - c["start"] >= 50.0 for c in out), out
    print("PASS retime_e14_zero_length_segment")

def test_retime_dense_mixed_cues_keep_duration():
    """Dense back-to-back cues: two anchored cues whose starts land 30ms
    apart — the end clamp must never shrink a cue below start + min(50ms,
    original duration); BOTH keep >= 0.05s, method stays mixed (NOT order),
    output valid."""
    asr = [
        {"start": 10000.0, "end": 12000.0, "text": "セリフです。0"},
        {"start": 10030.0, "end": 12030.0, "text": "セリフです。1"},
    ]
    sub = [
        {"start": 0.0, "end": 2000.0, "text": asr[0]["text"]},
        {"start": 30.0, "end": 2030.0, "text": asr[1]["text"]},
        {"start": 60.0, "end": 2060.0, "text": "はやい"},
    ]
    out, stats = o.retime_external_cues(sub, asr, "jpn", 200.0)
    assert out is not None and stats["method"] == "mixed", stats
    assert stats["anchors"] == 2, stats
    assert all(c["end"] - c["start"] >= 50.0 for c in out), out
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), starts
    print("PASS retime_dense_mixed_cues_keep_duration")


def test_retime_tiny_segment_filtered():
    """A 20ms whisper segment (end > start but < 100ms) is dropped by the
    sub-100ms ASR filter; order mapping works, no reject, real durations."""
    asr = [
        {"start": 5000.0, "end": 8000.0, "text": "スピーチです。0"},
        {"start": 9000.0, "end": 9020.0, "text": "tiny 20ms ghost"},
        {"start": 10000.0, "end": 13000.0, "text": "スピーチです。2"},
    ]
    sub = [{"start": i * 3000.0, "end": i * 3000.0 + 2500,
            "text": f"字幕の台詞です。{i}"} for i in range(3)]
    out, stats = o.retime_external_cues(sub, asr, "jpn", 200.0)
    assert out is not None and stats["method"] == "order", stats
    assert all(c["end"] - c["start"] >= 50.0 for c in out), out
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), starts
    print("PASS retime_tiny_segment_filtered")


def test_retime_jaadugar_shape_dense():
    """Jaadugar-shape repro: 10 dense back-to-back sub cues anchored with
    shifts ~+8.1s down to 0 (plus one SDH cue) — mixed mode survives the
    dense-cue end clamps, all durations >= 0.05s, monotonic, no reject."""
    asr = [{"start": 8100.0 + i * 30, "end": 8100.0 + i * 30 + 2500,
            "text": f"セリフの内容です。{i}"} for i in range(10)]
    sub = []
    for i in range(10):
        s = 1000.0 * i
        if i == 5:
            sub.append({"start": s, "end": s + 1000, "text": "♬ ♬"})
        else:
            sub.append({"start": s, "end": s + 1000, "text": asr[i]["text"]})
    out, stats = o.retime_external_cues(sub, asr, "jpn", 200.0)
    assert out is not None and stats["method"] == "mixed", stats
    assert stats["anchors"] == 9, stats
    assert all(c["end"] - c["start"] >= 50.0 for c in out), out
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), starts
    print("PASS retime_jaadugar_shape_dense")


def test_retime_eng_en_text_anchors():
    """eng sub + en ASR (movie audio is eng): texts match -> text anchors,
    every retimed start lands on its ASR cue, method text."""
    asr = [{"start": 5000.0 + i * 3000, "end": 5000.0 + i * 3000 + 2000,
            "text": f"This is the english speech line {i}."} for i in range(30)]
    sub = [{"start": i * 3000.0 - 8000, "end": i * 3000.0 - 8000 + 2500,
            "text": f"This is the english speech line {i}."} for i in range(30)]
    out, stats = o.retime_external_cues(sub, asr, "eng", 200.0, asr_lang="en")
    assert out is not None and stats["method"] == "text", stats
    assert stats["anchors"] == 30 and stats["matched_frac"] == 1.0, stats
    for i in range(30):
        assert abs(out[i]["start"] - asr[i]["start"]) < 1e-6, (i, out[i], asr[i])
    print("PASS retime_eng_en_text_anchors")


def test_retime_eng_ja_order():
    """eng sub + ja ASR (asr_lang='ja'): texts cannot match the reference —
    order-preserving mapping, never text-anchored (existing behavior)."""
    asr = [{"start": 5000.0 + i * 3000, "end": 5000.0 + i * 3000 + 2000,
            "text": f"日本語の音声です。{i}"} for i in range(30)]
    sub = [{"start": i * 3000.0 - 8000, "end": i * 3000.0 - 8000 + 2500,
            "text": f"This is the english subtitle line {i}."} for i in range(30)]
    out, stats = o.retime_external_cues(sub, asr, "eng", 200.0, asr_lang="ja")
    assert out is not None and stats["method"] == "order", stats
    assert stats["anchors"] == 0, stats
    starts = [c["start"] for c in out]
    assert all(b >= a for a, b in zip(starts, starts[1:])), starts
    print("PASS retime_eng_ja_order")


def test_retime_anchor_eng_movie():
    """Movie audio is eng: _retime_anchor_asr with sub_lang='eng' picks the
    eng stream via choose_source (target 'en') -> asr_lang 'en'; the ASR cache
    is keyed and transcribed as en (not ja)."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Die.Hard.1988.mp4")
    open(mkv, "w").close()
    eng_streams = o._AudioStreams(
        [{"index": 1, "codec_name": "ac3", "tags": {"language": "eng"},
          "channel_layout": "5.1", "duration": "7200.0"}]
    )
    eng_streams.format_duration = 7200.0
    cues = [{"start": 1000.0, "end": 2000.0, "text": "Yippee ki yay"}]
    saved = {}
    for n in ("probe_audio", "asr_cache_get", "extract_wav", "asr_cues", "asr_cache_put"):
        saved[n] = getattr(o, n)
    cache_calls = []
    o.probe_audio = lambda path: eng_streams
    o.asr_cache_get = lambda *a: cache_calls.append(a) or None
    o.extract_wav = lambda *a, **k: None
    o.asr_cues = lambda *a, **k: cues
    o.asr_cache_put = lambda *a, **k: cache_calls.append(a)
    cfg = build_cfg()
    cfg["TMP_DIR"] = d
    try:
        got, asr_lang = o._retime_anchor_asr(cfg, mkv, "eng")
        assert got == cues and asr_lang == "en", (got, asr_lang)
        sig = o.audio_stream_signature(eng_streams)
        assert cache_calls[0][:2] == (sig, "en"), cache_calls
        assert cache_calls[-1][:2] == (sig, "en"), cache_calls
        leftovers = [f for f in os.listdir(d) if f.startswith("retime_anchor_")]
        assert not leftovers, leftovers
    finally:
        for n, v in saved.items():
            setattr(o, n, v)
    print("PASS retime_anchor_eng_movie")


# ---------- movie track (radarr) ----------


def _run_pass_movie(
    tmp_dir,
    movies,
    wanted=None,
    state=None,
    infos=None,
    movie_library=True,
    regen=False,
    max_eps=8,
    target_langs=None,
    registry_rows=None,
    sidecar=None,
    ladder_kind="jpn",
):
    """Drive run_pass() with the movie sweep enabled (no HTTP, ffmpeg, or
    ASR). movies: list of dicts fed to get_movies (path = real local file).
    infos maps series ep_id -> get_episode() response. Returns (stats, mocks)."""
    cfg = build_cfg()
    cfg["TARGET_LANGS"] = target_langs or ["id"]
    cfg["TRANSLATE_API_KEY"] = "test-key"
    cfg["MAX_EPS_PER_RUN"] = max_eps
    cfg["TMP_DIR"] = tmp_dir
    cfg["MOVIE_LIBRARY"] = movie_library
    cfg["REGEN_LIBRARY"] = regen
    mocks = {
        "get_wanted_calls": [],
        "get_movies_calls": [],
        "probe": [],
        "submit": [],
        "ladder_submit": [],
        "sidecar_calls": [],
        "state_writes": [],
        "log": [],
    }
    saved = {}

    def save(name):
        saved[name] = getattr(o, name)

    for name in (
        "load_config",
        "consume_actions",
        "load_state",
        "get_wanted",
        "get_movies",
        "parse_exclusions",
        "get_episode",
        "probe_audio",
        "choose_source",
        "detect_ladder_source",
        "asr_cache_get",
        "extract_wav",
        "asr_cues",
        "asr_cache_put",
        "process_after_asr",
        "process_ladder",
        "run_upgrades",
        "log",
        "notify_hermes",
        "halt_on_error",
        "append_state",
        "target_sidecar_exists",
        "STATE_FILE",
        "REGISTRY_FILE",
    ):
        save(name)
    o.load_config = lambda: cfg
    o.consume_actions = lambda cfg_: set()
    o.load_state = lambda: list(state or [])
    o.get_wanted = lambda cfg_: mocks["get_wanted_calls"].append(1) or {
        "total": len(wanted or []),
        "data": wanted or [],
    }
    o.get_movies = lambda cfg_: mocks["get_movies_calls"].append(1) or {
        "total": len(movies or []),
        "data": movies or [],
    }
    o.parse_exclusions = lambda: set()
    o.get_episode = lambda cfg_, ep_id: (infos or {})[ep_id]
    o.probe_audio = lambda path: mocks["probe"].append(path) or [
        {
            "index": 1,
            "codec_name": "aac",
            "tags": {"language": "jpn"},
            "channels": 2,
            "duration": "100.0",
        }
    ]
    o.choose_source = lambda streams, lang: {
        "stream_index": 1,
        "asr_lang": "ja",
        "needs_translate": True,
        "src_lang": "jpn",
    }
    o.detect_ladder_source = lambda *a, **k: {
        "kind": ladder_kind,
        "source_path": "x.srt",
    }
    o.asr_cache_get = lambda *a: None
    o.extract_wav = lambda path, idx, out: open(out, "w").close()
    o.asr_cues = lambda cfg_, wav, lang: [
        {"start": 0, "end": 1000, "text": "Halo dunia."}
    ]
    o.asr_cache_put = lambda *a: None
    o.process_after_asr = lambda *a, **k: mocks["submit"].append(a) or "done"
    o.process_ladder = lambda *a, **k: mocks["ladder_submit"].append((a, k)) or "done"
    o.run_upgrades = lambda *a, **k: {"upgraded": 0, "checked": 0}
    o.log = lambda msg: mocks["log"].append(msg)
    o.notify_hermes = lambda *a, **k: None
    o.halt_on_error = lambda *a, **k: None
    o.append_state = lambda entry: mocks["state_writes"].append(entry)
    o.target_sidecar_exists = (
        lambda path: mocks["sidecar_calls"].append(path) or sidecar
    )
    o.STATE_FILE = os.path.join(tmp_dir, "state.jsonl")
    o.REGISTRY_FILE = os.path.join(tmp_dir, "subtitle_registry.jsonl")
    with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
        for row in registry_rows or []:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    try:
        stats = o.run_pass()
    finally:
        for name, val in saved.items():
            setattr(o, name, val)
    return stats, mocks


def test_movie_candidates():
    """movie_candidates: monitored+file movies become candidates; unmonitored,
    registered (stem, lang) and target-sidecar movies are skipped; duplicate
    radarrIds dedupe; result sorted by radarrId; fetch failure -> [] (never
    raises)."""
    import tempfile

    d = tempfile.mkdtemp()
    paths = {}
    for name in ("Zeta.mkv", "Alpha.mkv", "Unmon.mkv", "Reg.mkv", "Side.mkv"):
        p = os.path.join(d, name)
        open(p, "w").close()
        paths[name] = p
    movies = [
        {"radarrId": 7, "title": "Zeta", "monitored": True, "path": paths["Zeta.mkv"]},
        {"radarrId": 3, "title": "Alpha", "monitored": True, "path": paths["Alpha.mkv"]},
        {"radarrId": 5, "title": "Unmon", "monitored": False, "path": paths["Unmon.mkv"]},
        {"radarrId": 9, "title": "Reg", "monitored": True, "path": paths["Reg.mkv"]},
        {"radarrId": 9, "title": "RegDup", "monitored": True, "path": paths["Reg.mkv"]},
        {"radarrId": 11, "title": "Side", "monitored": True, "path": paths["Side.mkv"]},
    ]
    reg_stem = os.path.splitext(paths["Reg.mkv"])[0]
    side_stem = os.path.splitext(paths["Side.mkv"])[0]
    open(side_stem + ".id.srt", "w").close()
    saved_reg = o.REGISTRY_FILE
    saved_log = o.log
    saved_get = o.get_movies
    logs = []
    o.REGISTRY_FILE = os.path.join(d, "registry.jsonl")
    with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"stem": reg_stem, "lang": "id", "source": "asr"}) + "\n")
        fh.write(json.dumps({"stem": reg_stem, "lang": "en", "source": "jpn"}) + "\n")
        fh.write(json.dumps({"stem": side_stem, "lang": "en", "source": "eng"}) + "\n")
    o.log = lambda msg: logs.append(msg)
    o.get_movies = lambda cfg_: {"total": len(movies), "data": movies}
    try:
        cands = o.movie_candidates(build_cfg(), ["id", "en"])
        ids = [c["radarrId"] for c in cands]
        assert ids == [3, 7], ids
        by_id = {c["radarrId"]: c for c in cands}
        assert {m["code2"] for m in by_id[3]["missing_subtitles"]} == {"id", "en"}
        assert by_id[3]["movie"] is True and by_id[3]["movieTitle"] == "Alpha"
        assert by_id[3]["path"] == paths["Alpha.mkv"]
        assert by_id[3]["sonarrEpisodeId"] == 3
        assert any("movies: skip Reg [id] already registered (asr)" in l for l in logs), logs
        assert any("movies: skip Reg [en] already registered (jpn)" in l for l in logs), logs
        assert any(
            "movies: skip Side [id]: target-lang sidecar exists (Side.id.srt)" in l
            for l in logs
        ), logs
        assert not any("Unmon" in l for l in logs), logs
        # fetch failure: [] + log, never raises
        o.get_movies = lambda cfg_: (_ for _ in ()).throw(RuntimeError("bazarr down"))
        assert o.movie_candidates(build_cfg(), ["id"]) == []
        assert any("ERROR fetching movies list" in l for l in logs), logs
    finally:
        o.REGISTRY_FILE = saved_reg
        o.log = saved_log
        o.get_movies = saved_get
    print("PASS movie_candidates")


def test_movie_library_off_no_candidates():
    """MOVIE_LIBRARY off: run_pass never touches the movies API and never
    submits movie candidates."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Die.Hard.1988.mp4")
    open(mkv, "w").close()
    movies = [{"radarrId": 41, "title": "Die Hard", "monitored": True, "path": mkv}]
    stats, mocks = _run_pass_movie(d, movies, movie_library=False)
    assert mocks["get_movies_calls"] == [], mocks["get_movies_calls"]
    assert mocks["ladder_submit"] == [] and mocks["submit"] == []
    assert not any(l.startswith("movies ") for l in mocks["log"]), mocks["log"]
    assert stats["processed"] == 0 and stats["done"] == 0, stats
    print("PASS movie_library_off_no_candidates")


def test_movie_pass_processes_candidate():
    """MOVIE_LIBRARY on: the sweep feeds movie candidates into the pass; the
    movie branch probes, ladders, and submits process_ladder with
    movie_id=radarrId, ep_id=radarrId, series=title, tag=MOVIE, and an info
    dict carrying the container path (no Sonarr ids); every missing lang is
    processed."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv = os.path.join(d, "Die.Hard.1988.mp4")
    open(mkv, "w").close()
    movies = [{"radarrId": 41, "title": "Die Hard", "monitored": True, "path": mkv}]
    stats, mocks = _run_pass_movie(d, movies, target_langs=["id", "en"])
    assert stats["done"] == 2 and stats["failed"] == 0 and stats["skipped"] == 0, stats
    subs = mocks["ladder_submit"]
    assert len(subs) == 2, subs
    for args, kwargs in subs:
        assert kwargs.get("movie_id") == 41, kwargs
        assert args[2] == 41, "ep_id must be the movie id"
        assert args[4] == "Die Hard" and args[5] == "MOVIE", (args[4], args[5])
        info = args[7]
        assert info["episodeFile"]["path"] == mkv, info
        assert info["seriesId"] is None and info["sonarrSeriesId"] is None, info
    assert sorted(a[3] for a, _ in subs) == ["en", "id"], subs
    assert len(mocks["probe"]) == 2, mocks["probe"]
    assert not mocks["submit"], "ASR path must not run when the ladder hits"
    assert any("movies: process Die Hard [id]" in l for l in mocks["log"])
    assert any("movies: process Die Hard [en]" in l for l in mocks["log"])
    assert any("movies total=1, movie candidates=1" in l for l in mocks["log"])
    assert stats["movies_remaining"] == 1, stats
    print("PASS movie_pass_processes_candidate")


def test_state_kind_no_collision():
    """A series-kind done row for id 5 must NOT suppress the movie candidate
    with radarrId 5: done_keys/consec_errors/last_entry are (kind, episode,
    language) triples, so series and movie ids never collide."""
    import tempfile

    d = tempfile.mkdtemp()
    mkv5 = os.path.join(d, "Movie5.mp4")
    open(mkv5, "w").close()
    ep6 = os.path.join(d, "Ep6.mkv")
    open(ep6, "w").close()
    infos = {
        6: {
            "hasFile": True,
            "seasonNumber": 1,
            "episodeNumber": 6,
            "seriesId": 99,
            "episodeFile": {"path": ep6},
        }
    }
    wanted = [
        {
            "sonarrEpisodeId": 6,
            "seriesTitle": "TestShow",
            "missing_subtitles": [{"code2": "id"}],
        }
    ]
    state = [{"sonarrEpisodeId": 5, "language": "id", "status": "done"}]
    movies = [{"radarrId": 5, "title": "MovieFive", "monitored": True, "path": mkv5}]
    stats, mocks = _run_pass_movie(d, movies, wanted=wanted, state=state, infos=infos)
    assert stats["done"] == 2 and stats["skipped"] == 0, stats
    series_sub = [k for a, k in mocks["ladder_submit"] if k.get("movie_id") is None]
    movie_sub = [k for a, k in mocks["ladder_submit"] if k.get("movie_id") == 5]
    assert len(series_sub) == 1 and len(movie_sub) == 1, mocks["ladder_submit"]
    assert not any("already done (state)" in l for l in mocks["log"]), mocks["log"]
    print("PASS state_kind_no_collision")


def test_regen_skips_movie_rows():
    """REGEN_LIBRARY regen loop skips kind='movie' state rows: only the series
    done pair is regenerated, the movie pair never becomes a series regen
    candidate."""
    import tempfile

    d = tempfile.mkdtemp()
    infos = {6: _mk_regen_info(d, "Ep6.mkv", 6)}
    state = [
        {"sonarrEpisodeId": 5, "language": "id", "status": "done", "kind": "movie"},
        {"sonarrEpisodeId": 6, "language": "id", "status": "done", "seriesTitle": "TestShow"},
    ]
    stats, mocks = _run_pass_movie(
        d,
        [],
        state=state,
        infos=infos,
        movie_library=False,
        regen=True,
        ladder_kind="asr",
    )
    assert stats["done"] == 1 and stats["failed"] == 0, stats
    assert [a[2] for a in mocks["submit"]] == [6], mocks["submit"]
    assert mocks["ladder_submit"] == [], mocks["ladder_submit"]
    assert not any("regen: process S01E05" in l for l in mocks["log"]), mocks["log"]
    print("PASS regen_skips_movie_rows")


def test_upload_srt_movie_retry_204():
    """upload_srt_movie posts to /movies/subtitles with movieid/language/
    forced/hi params + multipart file; non-204 responses retry (3 attempts),
    then 204 returns."""
    codes = [500, 500, 204]
    calls = []

    class FakeResp:
        status_code = 200
        text = "err"

    class FakeRequests:
        def __init__(self, codes):
            self.codes = codes

        def post(self, url, params=None, headers=None, files=None, timeout=None):
            calls.append((url, dict(params or {}), files))
            r = FakeResp()
            r.status_code = self.codes.pop(0)
            return r

    saved = o.requests
    o.requests = FakeRequests(codes)
    try:
        cfg = {"BAZARR_URL": "http://x/api", "BAZARR_API_KEY": "k"}
        code = o.upload_srt_movie(cfg, 42, "id", b"x", filename="m.id.srt")
        assert code == 204, code
        assert len(calls) == 3, calls
        url, params, files = calls[0]
        assert url.endswith("/movies/subtitles"), url
        assert params == {
            "movieid": 42,
            "language": "id",
            "forced": "false",
            "hi": "false",
        }, params
        assert files["file"][0] == "m.id.srt", files
    finally:
        o.requests = saved
    print("PASS upload_srt_movie_retry_204")


def test_upload_srt_movie_all_fail_last_code():
    """All 3 attempts non-204: last status code returned."""
    codes = [500, 500, 500]
    calls = []

    class FakeResp:
        status_code = 200
        text = "err"

    class FakeRequests:
        def __init__(self, codes):
            self.codes = codes

        def post(self, url, params=None, headers=None, files=None, timeout=None):
            calls.append(url)
            r = FakeResp()
            r.status_code = self.codes.pop(0)
            return r

    saved = o.requests
    o.requests = FakeRequests(codes)
    try:
        cfg = {"BAZARR_URL": "http://x/api", "BAZARR_API_KEY": "k"}
        code = o.upload_srt_movie(cfg, 42, "id", b"x")
        assert code == 500, code
        assert len(calls) == 3, calls
    finally:
        o.requests = saved
    print("PASS upload_srt_movie_all_fail_last_code")


def test_jellyfin_refresh_movie_item_type():
    """Movies refresh searches IncludeItemTypes=Movie (series default stays
    Episode); the path match still gates the refresh POST."""
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
            calls.append(("GET", dict(params or {})))
            return FakeResp(
                {
                    "Items": [
                        {"Id": "m1",
                         "Path": "/media/jellyfin/radarr-movies/Die.Hard.1988/Die.Hard.1988.mp4"}
                    ]
                }
            )

        @staticmethod
        def post(url, json=None, headers=None, timeout=None):
            calls.append(("POST", url))
            return FakeResp(None, 204)

    saved_requests = o.requests
    o.requests = FakeRequests
    try:
        cfg = {"JELLYFIN_API_KEY": "k123", "JELLYFIN_URL": "http://jf:8096"}
        o.jellyfin_refresh(
            cfg,
            "/mnt/nas/share/media/jellyfin/radarr-movies/Die.Hard.1988/Die.Hard.1988.mp4",
            "Die Hard",
            item_type="Movie",
        )
        deadline = time.time() + 5
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert len(calls) == 2, calls
        m1 = calls[0]
        assert m1[0] == "GET" and m1[1]["IncludeItemTypes"] == "Movie", m1
        assert calls[-1][0] == "POST" and calls[-1][1].endswith("/Items/m1/Refresh")
    finally:
        o.requests = saved_requests
    print("PASS jellyfin_refresh_movie_item_type")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL DRY TESTS PASSED")
