import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests import HermeticStateMixin

import control_api_v2 as api2
import orchestrator as o


class TestLanguageRegistryAdversarial(HermeticStateMixin):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="asrsub-adversarial-")
        self.registry = os.path.join(self.tmp, "registry.jsonl")

    def test_sweep_legacy_sidecars_do_not_create_parallel_outputs(self):
        media = os.path.join(self.tmp, "Show.mkv")
        open(media, "wb").close()
        stem = os.path.splitext(media)[0]
        for suffix in ("jpn", "eng.forced", "ind.hi"):
            open(f"{stem}.{suffix}.srt", "wb").close()
        streams = [
            {"index": 4, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "jpn"}},
            {"index": 5, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "eng"}},
            {"index": 6, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "ind"}},
        ]
        ffmpeg = []

        def run(cmd, **kwargs):
            ffmpeg.append(cmd)
            return type("Result", (), {"returncode": 1, "stderr": "unexpected ffmpeg"})()

        cfg = {"TARGET_LANGS": ["ja", "en", "id"], "JELLYFIN_MEDIA_ROOT": self.tmp}
        real_isdir = o.os.path.isdir

        def isdir(path):
            return False if path == "/mnt/nas/share/media/jellyfin" else real_isdir(path)

        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(
            o, "_subtitle_streams", return_value=streams
        ), patch.object(o.os.path, "isdir", side_effect=isdir), patch.object(
            o.subprocess, "run", side_effect=run
        ):
            result = o.run_embedded_srt_sweep(cfg, budget=10)

        self.assertEqual(result["extracted"], 0)
        self.assertEqual(len(ffmpeg), 1)
        self.assertEqual(
            sorted(os.path.basename(path) for path in os.listdir(self.tmp) if path.endswith(".srt")),
            ["Show.eng.forced.srt", "Show.ind.hi.srt", "Show.jpn.srt"],
        )
        self.assertFalse(os.path.exists(self.registry))

    def test_stale_movie_registry_row_does_not_suppress_candidate(self):
        media = os.path.join(self.tmp, "Movie.mkv")
        open(media, "wb").close()
        stem = os.path.splitext(media)[0]
        with open(self.registry, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"stem": stem, "lang": "ja", "source": "asr", "source_path": "", "source_hash": ""}) + "\n")
        movies = {"total": 1, "data": [{"radarrId": 1, "title": "Movie", "path": media, "monitored": True}]}
        cfg = {"TARGET_LANGS": ["ja"], "BAZARR_URL": "unused", "BAZARR_API_KEY": "x"}
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value=movies):
            candidates = o.movie_candidates(cfg, {"ja"})
        self.assertEqual(candidates[0]["missing_subtitles"], [{"code2": "ja"}])
        self.assertEqual(json.loads(open(self.registry, encoding="utf-8").readline())["source"], "asr")

    def test_jpn_state_row_is_done_for_ja_target_when_not_missing(self):
        media = os.path.join(self.tmp, "Movie.mkv")
        open(media, "wb").close()
        with open(o.STATE_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "movie", "sonarrEpisodeId": 2, "language": "jpn", "status": "done", "ts": "2026-01-01T00:00:00Z"}) + "\n")
        movies = {"total": 1, "data": [{"radarrId": 2, "title": "Movie", "path": media, "monitored": True, "missing_subtitles": []}]}
        cfg = {"TARGET_LANGS": ["ja"], "BAZARR_URL": "unused", "BAZARR_API_KEY": "x"}
        with patch.object(o, "get_movies", return_value=movies), patch.object(o, "REGISTRY_FILE", self.registry):
            self.assertEqual(o.movie_candidates(cfg, {"ja"}), [])

    def test_run_pass_normalizes_bazarr_missing_aliases_before_ordering(self):
        cfg = {"TARGET_LANGS": ["ja"], "MAX_EPS_PER_RUN": 1, "MOVIE_LIBRARY": False, "REGEN_LIBRARY": False, "TMP_DIR": self.tmp, "TRANSLATE_API_KEY": "x"}
        item = {"sonarrEpisodeId": 42, "missing_subtitles": [{"code2": "jpn"}]}
        captured = []

        def order(config, wanted, movies, regen, maximum):
            captured.extend(wanted)
            return [], 0

        with patch.object(o, "load_config", return_value=cfg), patch.object(o, "get_wanted", return_value={"total": 1, "data": [item]}), patch.object(o, "_order_pass_candidates", side_effect=order), patch.object(o, "run_upgrades", return_value={"upgraded": 0, "checked": 0}), patch.object(o, "run_timeline_recalc", return_value={"repaired": 0, "failed": []}), patch.object(o, "run_embedded_srt_sweep", return_value={"extracted": 0, "scanned": 0, "failed": []}), patch.object(o, "run_jimaku_hunt", return_value={}), patch.object(o, "notify_webhook"):
            o.run_pass()
        self.assertEqual(captured, [item])

    def test_timeline_recalc_considers_canonical_ja_registry_row(self):
        media = os.path.join(self.tmp, "Show.mkv")
        sidecar = os.path.join(self.tmp, "Show.ja.srt")
        open(media, "wb").close()
        with open(sidecar, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\nline\n")
        stem = os.path.splitext(media)[0]
        row = {(stem, "ja"): {"stem": stem, "lang": "ja", "source": "jpn"}}
        with patch.object(o, "load_registry", return_value=row), patch.object(o, "media_duration_s", return_value=1.0), patch.object(o, "validate_srt_timeline", return_value=(True, "ok")):
            result = o.run_timeline_recalc({"TIMELINE_RECALC_BUDGET": 5})
        self.assertEqual(result["checked"], 1)

    def test_renamed_legacy_external_sidecar_reconciles_by_same_hash(self):
        media = os.path.join(self.tmp, "Show.mkv")
        legacy = os.path.join(self.tmp, "Show.jpn.srt")
        canonical = os.path.join(self.tmp, "Show.ja.srt")
        open(media, "wb").close()
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\nline\n")
        digest = o.file_sha256(legacy)
        with patch.object(o, "REGISTRY_FILE", self.registry):
            o.registry_upsert(os.path.splitext(media)[0], "jpn", "jpn", source_path=legacy, source_hash=digest, source_kind="external", audio_id="audio", effective_timeline=[[0, 1000]], timeline_kind="raw", retime_status="rejected", retime_policy_version=o.RETIME_POLICY_VERSION)
            os.rename(legacy, canonical)
            with patch.object(o, "probe_audio", return_value=[]), patch.object(o, "audio_stream_signature", return_value="audio"):
                self.assertTrue(o._sidecar_trusted(os.path.splitext(media)[0], "ja", media, canonical))
            rows = [json.loads(line) for line in open(self.registry, encoding="utf-8")]
        self.assertEqual(rows[-1]["lang"], "ja")
        self.assertEqual(rows[-1]["source_path"], canonical)
        self.assertEqual(rows[-1]["source_hash"], digest)

    def test_control_api_normalizes_all_supported_aliases(self):
        for alias, canonical in (("jp", "ja"), ("jpn", "ja"), ("eng", "en"), ("enm", "en"), ("ind", "id")):
            self.assertEqual(api2.LANG_NORM.get(alias), canonical)


if __name__ == "__main__":
    unittest.main()
