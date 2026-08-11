#!/usr/bin/env python3
"""Ladder source-detection self-test against the real NFS library.

Probes a real mkv + its {stem}.jpn.srt sidecar (Frieren S01E01) and prints the
adequacy-gate verdict for the jpn source, plus the audio-stream signature used
for the ASR cache key. Requires ffprobe and the NFS mount.

Run: python3 pipeline/ladder_selftest.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orchestrator as o

VIDEO = (
    "/mnt/nas/share/media/jellyfin/sonarr-tv-shows/"
    "Frieren - Beyond Journey's End/Season 01/"
    "Frieren - Beyond Journey's End - S01E01 - The Journey's End Bluray-1080p Remux.mkv"
)
JPN = VIDEO[: -len(".mkv")] + ".jpn.srt"


def main():
    missing = [p for p in (VIDEO, JPN) if not os.path.isfile(p)]
    if missing:
        print(f"SKIP: missing on NFS: {missing}")
        return 0

    cfg = o.load_config()
    cfg["LADDER_MIN_CUES"] = os.environ.get("LADDER_MIN_CUES", "40")
    cfg["LADDER_MIN_CHARS"] = os.environ.get("LADDER_MIN_CHARS", "1500")
    cfg["LADDER_MIN_CJK"] = os.environ.get("LADDER_MIN_CJK", "0.6")
    cfg["LADDER_SPAN_TOLERANCE"] = os.environ.get("LADDER_SPAN_TOLERANCE", "0.15")

    duration = o.media_duration_s(VIDEO)
    print(f"video:       {os.path.basename(VIDEO)}")
    print(f"duration:    {duration:.1f}s" if duration else "duration:    unknown")

    print(f"sidecar:     {os.path.basename(JPN)}")
    verdict = o.assess_source_file(cfg, JPN, "jpn", duration)
    print(f"gate verdict: {'PASS' if verdict['ok'] else 'REJECT'} ({verdict['reason']})")
    if verdict.get("cues"):
        cues = verdict["cues"]
        text = "".join(c["text"] for c in cues)
        cjk = len(o.re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", o.re.sub(r"\s", "", text)))
        latin = len(o.re.findall(r"[A-Za-z]", o.re.sub(r"\s", "", text)))
        print(f"  cues={len(cues)} chars={len(o.re.sub(r'\\s', '', text))} "
              f"cjk_ratio={cjk / max(len(o.re.sub(r'\\s', '', text)), 1):.3f} "
              f"latin_ratio={latin / max(len(o.re.sub(r'\\s', '', text)), 1):.3f} "
              f"span_s={verdict['span_s']}")
        print(f"  source_hash={verdict['source_hash'][:16]}…")
        sample = o.clean_ass_text(cues[0]["text"])
        print(f"  first clean cue: {sample!r}")

    print("full ladder detection (kind / source_path):")
    ladder = o.detect_ladder_source(cfg, VIDEO, "id", "/tmp", ep_id=1, series_id=1)
    print(f"  {ladder['kind']}  {ladder['source_path']}")

    print("audio stream signature:")
    try:
        streams = o.probe_audio(VIDEO)
        print(f"  audio_id={o.audio_stream_signature(streams)} (streams={len(streams)}, "
              f"fmt_dur={streams.format_duration})")
        for s in streams:
            print(
                f"    idx={s.get('index')} codec={s.get('codec_name')} "
                f"lang={(s.get('tags') or {}).get('language')} "
                f"layout={s.get('channel_layout') or s.get('channels')} "
                f"dur={s.get('duration')}"
            )
    except Exception as exc:
        print(f"  probe failed: {exc}")

    print("VERDICT:", "LADDER_SELFTEST_PASS" if verdict["ok"] else "LADDER_SELFTEST_REJECT")
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
