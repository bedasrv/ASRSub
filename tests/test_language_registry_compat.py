import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests import HermeticStateMixin

import orchestrator as o


class TestLanguageRegistryCompatibility(HermeticStateMixin):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="asrsub-language-")
        self.registry = os.path.join(self.tmp, "subtitle_registry.jsonl")

    def test_target_aliases_deduplicate_and_unknowns_remain_explicit(self):
        env = os.path.join(self.tmp, "pipeline.env")
        with open(env, "w", encoding="utf-8") as fh:
            fh.write("TARGET_LANGS= ja , JP! , jpn, ind, ID, en, ENG, enm, xx\n")
        with patch.object(o, "ENV_FILE", env), patch.object(
            o, "OVERRIDE_FILE", os.path.join(self.tmp, "overrides.json")
        ):
            cfg = o.load_config()
        self.assertEqual(cfg["TARGET_LANGS"], ["ja", "id", "en", "xx"])

    def test_embedded_aliases_and_enm_match_real_picker(self):
        streams = [
            {"index": 1, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "jpn"}},
            {"index": 2, "codec_type": "subtitle", "codec_name": "ssa", "tags": {"language": "ind"}},
            {"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "enm"}},
        ]
        self.assertEqual(o._pick_best_subtitle(streams, "ja")["index"], 1)
        self.assertEqual(o._pick_best_subtitle(streams, "id")["index"], 2)
        self.assertEqual(o._pick_best_subtitle(streams, "en")["index"], 3)

    def test_extraction_creates_only_canonical_outputs_and_records(self):
        media = os.path.join(self.tmp, "Show.mkv")
        open(media, "wb").close()
        streams = [
            {"index": 7, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "jpn"}},
            {"index": 8, "codec_type": "subtitle", "codec_name": "ssa", "tags": {"language": "ind"}},
            {"index": 9, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "enm"}},
        ]
        maps = []

        def fake_run(cmd, **kwargs):
            maps.append(cmd[cmd.index("-map") + 1])
            with open(cmd[-1], "w", encoding="utf-8") as fh:
                fh.write("1\n00:00:00,000 --> 00:00:01,000\nline\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(
            o, "load_config", return_value={"TARGET_LANGS": ["ja", "ind", "enm"]}
        ), patch.object(o, "_subtitle_streams", return_value=streams), patch.object(
            o, "probe_audio", return_value=[]
        ), patch.object(o, "map_path", side_effect=lambda path: path), patch.object(
            o.subprocess, "run", side_effect=fake_run
        ):
            o.extract_subtitle_sidecars(media)

        stem = os.path.splitext(media)[0]
        self.assertEqual(maps, ["0:7", "0:8", "0:9"])
        for lang in ("ja", "id", "en"):
            path = f"{stem}.{lang}.hi.srt"
            self.assertTrue(os.path.isfile(path), path)
            self.assertIn("line", open(path, encoding="utf-8").read())
        self.assertFalse(os.path.exists(f"{stem}.jpn.srt"))
        self.assertFalse(os.path.exists(f"{stem}.ind.srt"))
        self.assertFalse(os.path.exists(f"{stem}.enm.srt"))
        rows = [json.loads(line) for line in open(self.registry, encoding="utf-8")]
        self.assertEqual({row["lang"] for row in rows}, {"ja", "id", "en"})

    def test_generic_sidecar_guard_and_movie_candidates_have_no_false_missing(self):
        media = os.path.join(self.tmp, "Movie.mkv")
        open(media, "wb").close()
        stem = os.path.splitext(media)[0]
        for suffix in ("jpn.hi", "enm.forced", "ind.hi.forced"):
            open(f"{stem}.{suffix}.srt", "wb").close()
        self.assertIsNone(o.target_sidecar_exists(media))
        cfg = {"TARGET_LANGS": ["ja", "en", "id"], "BAZARR_URL": "http://unused", "BAZARR_API_KEY": "x"}
        movies = {"total": 1, "data": [{"radarrId": 1, "title": "Movie", "path": media, "monitored": True}]}
        with patch.object(o, "get_movies", return_value=movies), patch.object(o, "REGISTRY_FILE", self.registry):
            candidates = o.movie_candidates(cfg, {"ja", "en", "id"})
            self.assertEqual(len(candidates), 1)
            self.assertEqual({x["code2"] for x in candidates[0]["missing_subtitles"]}, {"en", "id"})

    def test_registry_aliases_are_one_latest_canonical_record(self):
        stem = os.path.join(self.tmp, "Show")
        rows = [
            {"stem": stem, "lang": "jpn", "source": "old", "source_hash": "old"},
            {"stem": stem, "lang": "ja", "source": "new", "source_hash": "new"},
            {"stem": stem, "lang": "enm", "source": "english", "source_hash": "e"},
        ]
        with open(self.registry, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        with patch.object(o, "REGISTRY_FILE", self.registry):
            loaded = o.load_registry()
            self.assertEqual(o.registry_get(stem, "jpn")["source"], "new")
            self.assertEqual(o.registry_by_episode(42, "ja"), None)
        self.assertEqual(set(loaded), {(stem, "ja"), (stem, "en")})

    def test_registry_upsert_and_delete_use_canonical_alias(self):
        stem = os.path.join(self.tmp, "Show")
        with patch.object(o, "REGISTRY_FILE", self.registry):
            o.registry_upsert(stem, "jpn", "embedded", ep_id=42, source_hash="h")
            rows = [json.loads(line) for line in open(self.registry, encoding="utf-8")]
            self.assertEqual(rows[0]["lang"], "ja")
            self.assertEqual(o.registry_get(stem, "ja")["lang"], "ja")
            self.assertEqual(o.registry_by_episode(42, "jpn")["lang"], "ja")
            o.registry_delete(stem=stem, lang="ja")
            self.assertEqual(open(self.registry, encoding="utf-8").read(), "")

    def test_stale_registry_path_is_not_trusted_but_history_remains(self):
        media = os.path.join(self.tmp, "Show.mkv")
        sidecar = os.path.join(self.tmp, "Show.jpn.srt")
        open(media, "wb").close()
        with open(sidecar, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\nline\n")
        digest = o.file_sha256(sidecar)
        with patch.object(o, "REGISTRY_FILE", self.registry):
            o.registry_upsert(os.path.splitext(media)[0], "jpn", "embedded", source_path=os.path.join(self.tmp, "gone.srt"), source_hash=digest, source_kind="embedded", audio_id="audio")
            with patch.object(o, "probe_audio", return_value=[]), patch.object(o, "audio_stream_signature", return_value="audio"):
                self.assertFalse(o._sidecar_trusted(os.path.splitext(media)[0], "jpn", media, sidecar))
            self.assertEqual(len(open(self.registry, encoding="utf-8").readlines()), 1)

    def test_missing_source_is_reported_as_missing_not_done(self):
        media = os.path.join(self.tmp, "Movie.mkv")
        open(media, "wb").close()
        cfg = {"TARGET_LANGS": ["ja"], "BAZARR_URL": "http://unused", "BAZARR_API_KEY": "x"}
        movies = {"total": 1, "data": [{"radarrId": 7, "title": "Movie", "path": media}]}
        with patch.object(o, "get_movies", return_value=movies), patch.object(o, "REGISTRY_FILE", self.registry):
            candidates = o.movie_candidates(cfg, {"ja"})
        self.assertEqual(candidates[0]["missing_subtitles"], [{"code2": "ja"}])


if __name__ == "__main__":
    unittest.main()
