import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tests import HermeticStateMixin

import control_api_v2 as api2
import orchestrator as o


class TestLanguageRegistryAdversarial2(HermeticStateMixin):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="asrsub-adversarial2-")
        self.registry = os.path.join(self.tmp, "registry.jsonl")

    def _video(self, name="Show.mkv"):
        path = os.path.join(self.tmp, name)
        open(path, "wb").close()
        return path

    def test_delete_episode_removes_all_alias_and_flag_variants_only(self):
        media = self._video("Show.mkv")
        stem = os.path.splitext(media)[0]
        wanted = [
            "Show.jpn.srt", "Show.jpn.hi.srt", "Show.jpn.forced.srt",
            "Show.jpn.forced.hi.srt", "Show.eng.srt", "Show.eng.hi.forced.srt",
            "Show.enm.forced.hi.srt", "Show.ind.srt", "Show.ind.hi.forced.srt",
            "Show.ja.srt", "Show.en.srt", "Show.id.hi.srt",
        ]
        keep = ["Show.fr.srt", "Show.jpn.srt.bak", "Other.jpn.srt"]
        for name in wanted + keep:
            open(os.path.join(self.tmp, name), "wb").close()
        cfg = {"TARGET_LANGS": ["ja", "en", "id"], "TMP_DIR": self.tmp, "BAZARR_URL": "unused", "BAZARR_API_KEY": "x"}
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(
            o, "get_episode", return_value={"episodeFile": {"path": media}}
        ), patch.object(o, "_bazarr_wanted_refill"):
            deleted = o._delete_episode_subtitles(cfg, 10, langs=["ja", "en", "id"])
        self.assertEqual({os.path.basename(path) for path in deleted}, {n for n in wanted if ".forced" not in n})
        self.assertTrue(all(not os.path.exists(os.path.join(self.tmp, name)) for name in wanted if ".forced" not in name))
        self.assertTrue(all(os.path.exists(os.path.join(self.tmp, name)) for name in keep))

    def test_delete_movie_removes_legacy_aliases(self):
        media = self._video("Movie.mkv")
        names = ["Movie.jpn.hi.forced.srt", "Movie.eng.forced.srt", "Movie.enm.hi.srt", "Movie.ind.forced.hi.srt"]
        for name in names:
            open(os.path.join(self.tmp, name), "wb").close()
        cfg = {"TARGET_LANGS": ["ja", "en", "id"], "TMP_DIR": self.tmp, "BAZARR_URL": "unused", "BAZARR_API_KEY": "x"}
        movies = {"data": [{"radarrId": 22, "path": media}]}
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(
            o, "get_movies", return_value=movies
        ), patch.object(o, "_bazarr_wanted_refill"):
            deleted = o._delete_movie_subtitles(cfg, 22, langs=["ja", "en", "id"])
        self.assertEqual({os.path.basename(path) for path in deleted}, {n for n in names if ".forced" not in n})
        self.assertTrue(all(os.path.exists(os.path.join(self.tmp, n)) for n in names if ".forced" in n))

    def test_recover_fileless_movies_accepts_legacy_aliases_and_clears_missing(self):
        complete = self._video("Complete.mkv")
        missing = self._video("Missing.mkv")
        for suffix in ("jpn.hi", "enm.forced", "ind.hi.forced"):
            open(os.path.join(self.tmp, f"Complete.{suffix}.srt"), "wb").close()
        state_rows = [
            {"kind": "movie", "sonarrEpisodeId": 1, "language": "ja", "status": "done"},
            {"kind": "movie", "sonarrEpisodeId": 2, "language": "en", "status": "done"},
        ]
        reg_rows = [
            {"stem": os.path.splitext(complete)[0], "lang": "jpn", "episode_id": 1, "source": "asr"},
            {"stem": os.path.splitext(missing)[0], "lang": "eng", "episode_id": 2, "source": "asr"},
        ]
        with open(o.STATE_FILE, "w", encoding="utf-8") as fh:
            for row in state_rows:
                fh.write(json.dumps(row) + "\n")
        with open(self.registry, "w", encoding="utf-8") as fh:
            for row in reg_rows:
                fh.write(json.dumps(row) + "\n")
        cfg = {"TARGET_LANGS": ["ja", "en", "id"]}
        movies = {"data": [{"radarrId": 1, "title": "Complete", "path": complete}, {"radarrId": 2, "title": "Missing", "path": missing}]}
        with patch.object(o, "STATE_FILE", o.STATE_FILE), patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value=movies), patch.object(o, "_bazarr_wanted_refill"):
            result = o.recover_fileless_movies(cfg)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["cleared"], {1: ["en", "id"], 2: ["ja", "en", "id"]})
        remaining_state = [json.loads(line) for line in open(o.STATE_FILE, encoding="utf-8") if line.strip()]
        self.assertEqual({row["sonarrEpisodeId"] for row in remaining_state}, {1})
        remaining_registry = [json.loads(line) for line in open(self.registry, encoding="utf-8") if line.strip()]
        self.assertEqual({row["episode_id"] for row in remaining_registry}, {1})

    def test_pathless_legacy_asr_row_needs_marker_not_foreign_sidecar(self):
        media = self._video()
        stem = os.path.splitext(media)[0]
        foreign = f"{stem}.ja.srt"
        with open(foreign, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\nforeign\n")
        row = {(stem, "ja"): {"stem": stem, "lang": "ja", "source": "asr", "source_path": "", "source_hash": ""}}
        self.assertIsNone(o._registry_current_row(stem, "ja", media_path=media, row=row[(stem, "ja")]))
        self.assertFalse(o.sub_is_ai_owned(row, 1, "jpn", media))
        with open(foreign, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\nAI-generated by ASRSub\ntext\n")
        self.assertTrue(o.sub_is_ai_owned({}, 1, "ja", media))

    def test_process_after_asr_records_verified_written_target(self):
        media = self._video()
        generated = os.path.join(self.tmp, "generated.srt")
        cfg = {"BAZARR_URL": "http://bazarr/api", "BAZARR_API_KEY": "k", "TARGET_LANGS": ["ja"]}
        info = {"episodeFile": {"path": media}, "seriesId": 4, "title": "Show"}
        response = MagicMock(status_code=204)
        response.text = ""
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(
            o.requests, "post", return_value=response
        ), patch.object(o, "jellyfin_refresh"), patch.object(o, "notify_webhook"):
            result = o.process_after_asr(
                cfg, "", 8, "ja", "Show", "S01E01", "asr", 0,
                [{"start": 0, "end": 1000, "text": "text"}],
                {"needs_translate": False}, info, "", generated,
            )
        self.assertEqual(result, "done")
        target = f"{os.path.splitext(media)[0]}.ja.hi.srt"
        self.assertTrue(os.path.isfile(target))
        rows = [json.loads(line) for line in open(self.registry, encoding="utf-8")]
        self.assertEqual(rows[-1]["lang"], "ja")
        self.assertEqual(rows[-1]["source_path"], target)
        self.assertEqual(rows[-1]["source_hash"], o.file_sha256(target))

    def test_control_registry_and_movie_remaining_use_canonical_aliases(self):
        media = self._video("Movie.mkv")
        stem = os.path.splitext(media)[0]
        rows = [
            {"stem": stem, "lang": "jpn", "source": "old"},
            {"stem": stem, "lang": "ja", "source": "new"},
            {"stem": stem, "lang": "enm", "source": "english"},
        ]
        with open(self.registry, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        env = os.path.join(self.tmp, "pipeline.env")
        with open(env, "w", encoding="utf-8") as fh:
            fh.write("TARGET_LANGS=jpn,ind,enm\n")
        api = api2.ControlAPIv2({"ENV_FILE": env, "OVERRIDE_FILE": os.path.join(self.tmp, "overrides.json"), "REGISTRY_FILE": self.registry, "STATE_FILE": os.path.join(self.tmp, "state.jsonl"), "NAS_MEDIA_ROOT": self.tmp})
        registry = api._registry()
        self.assertEqual(len(registry), 2)
        self.assertEqual({row["lang"] for row in registry}, {"ja", "en"})
        with open(os.path.join(self.tmp, "Movie.enm.forced.srt"), "wb") as fh:
            fh.write(b"foreign")
        movies = {"data": [{"radarrId": 3, "path": media, "monitored": True, "missing_subtitles": []}]}
        self.assertEqual(api._movies_remaining_local(movies), 1)


if __name__ == "__main__":
    unittest.main()
