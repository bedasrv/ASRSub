import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tests import HermeticStateMixin

import orchestrator as o


class TestPermanentSubtitlePolicyRed(HermeticStateMixin):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="asrsub-policy-red-")
        self.media = os.path.join(self.tmp, "Show.mkv")
        open(self.media, "wb").close()
        self.stem = os.path.splitext(self.media)[0]
        self.cfg = {"BAZARR_URL": "http://bazarr/api", "BAZARR_API_KEY": "key", "TARGET_LANGS": ["ja", "id", "en"], "TMP_DIR": self.tmp, "JELLYFIN_MEDIA_ROOT": self.tmp}

    def test_uploads_request_hi_true_and_forced_false(self):
        response = MagicMock(status_code=204, text="")
        calls = []
        def ensure(_cfg, _media, _lang, _body, **_kw):
            path = self.stem + ".ja.hi.srt"
            with open(path, "wb") as fh: fh.write(b"x")
            return path, True
        with patch.object(o.requests, "post", side_effect=lambda url, params=None, **kw: (calls.append(params), response)[1]), patch.object(o, "_ensure_sidecar_on_disk", side_effect=ensure):
            self.assertEqual(o.upload_srt(self.cfg, 1, 2, "jpn", b"x", media_path=self.media), 204)
            self.assertEqual(o.upload_srt_movie(self.cfg, 3, "jpn", b"x", media_path=self.media), 204)
        self.assertEqual(len(calls), 2)
        for params in calls:
            self.assertEqual(params["language"], "ja")
            self.assertEqual(params["hi"], "true")
            self.assertEqual(params["forced"], "false")

    def test_upload_srt_preserves_concurrent_canonical_winner(self):
        canonical = self.stem + ".ja.hi.srt"
        submitted = b"1\n00:00:00,000 --> 00:00:01,000\nSubmitted.\n"
        winner = b"1\n00:00:00,000 --> 00:00:01,000\nConcurrent winner.\n"

        def post(_url, **_kwargs):
            with open(canonical, "wb") as fh:
                fh.write(winner)
            return MagicMock(status_code=204, text="")

        with patch.object(o.requests, "post", side_effect=post):
            result = o.upload_srt(self.cfg, 1, 2, "ja", submitted, media_path=self.media)
        self.assertEqual(result, 204)
        with open(canonical, "rb") as fh:
            self.assertEqual(fh.read(), winner)

    def test_upload_fallback_writes_only_canonical_hi_target(self):
        path, wrote = o._ensure_sidecar_on_disk(self.cfg, self.media, "JPN", b"generated", retries=1, delay=0)
        self.assertEqual(path, self.stem + ".ja.hi.srt")
        self.assertTrue(wrote)
        self.assertTrue(os.path.isfile(self.stem + ".ja.hi.srt"))
        self.assertFalse(os.path.exists(self.stem + ".ja.srt"))

    def test_upload_fallback_does_not_overwrite_regular_or_forced_siblings(self):
        regular, forced = self.stem + ".ja.srt", self.stem + ".ja.forced.srt"
        with open(regular, "wb") as fh: fh.write(b"regular")
        with open(forced, "wb") as fh: fh.write(b"forced")
        path, wrote = o._ensure_sidecar_on_disk(self.cfg, self.media, "ja", b"new", retries=1, delay=0)
        self.assertEqual(path, self.stem + ".ja.hi.srt")
        self.assertTrue(wrote)
        self.assertEqual(open(regular, "rb").read(), b"regular")
        self.assertEqual(open(forced, "rb").read(), b"forced")

    def test_process_ladder_preserves_existing_canonical_winner(self):
        source = {
            "kind": "jpn",
            "source_path": self.stem + ".jpn.srt",
            "source_hash": "source",
            "cues": [{"start": 0, "end": 1000, "text": "Japanese source."}],
            "tmp": False,
        }
        info = {"episodeFile": {"path": self.media}, "seriesId": 1, "title": "Show"}
        canonical = self.stem + ".ja.hi.srt"
        winner = b"1\n00:00:00,000 --> 00:00:01,000\nExisting winner.\n"
        with open(canonical, "wb") as fh:
            fh.write(winner)
        with patch.object(o, "translate_texts", return_value=[(0, 1, "Generated text.")]), patch.object(
            o, "upload_srt", return_value=204
        ), patch.object(o, "jellyfin_refresh"), patch.object(o, "notify_webhook"), patch.object(
            o, "registry_get", return_value=None
        ), patch.object(o, "registry_upsert"), patch.object(o, "append_state"):
            result = o.process_ladder(self.cfg, "key", 2, "ja", "Show", "S01E01", source, info)
        self.assertEqual(result, "done")
        with open(canonical, "rb") as fh:
            self.assertEqual(fh.read(), winner)

    def test_process_after_asr_preserves_concurrent_canonical_winner(self):
        canonical = self.stem + ".ja.hi.srt"
        generated = b"1\n00:00:00,000 --> 00:00:01,000\nGenerated.\n"
        winner = b"1\n00:00:00,000 --> 00:00:01,000\nConcurrent winner.\n"
        srt_path = os.path.join(self.tmp, "generated.srt")
        info = {"episodeFile": {"path": self.media}, "seriesId": 1, "title": "Show"}
        decision = {"needs_translate": False, "asr_lang": "ja"}
        cues = [{"start": 0, "end": 1000, "text": "ASR text."}]

        def write_with_winner(_cues, _texts, path, header=None):
            with open(path, "wb") as fh:
                fh.write(generated)
            with open(canonical, "wb") as fh:
                fh.write(winner)

        with patch.object(o, "write_srt", side_effect=write_with_winner), patch.object(
            o, "upload_srt", return_value=204
        ), patch.object(o, "jellyfin_refresh"), patch.object(o, "registry_upsert"), patch.object(
            o, "append_state"
        ), patch.object(o, "notify_webhook"), patch.object(o, "notify_hermes"), patch.object(
            o, "halt_on_error"
        ):
            result = o.process_after_asr(
                self.cfg, "key", 2, "ja", "Show", "S01E01", "asr", 0,
                cues, decision, info, "", srt_path
            )
        self.assertEqual(result, "done")
        with open(canonical, "rb") as fh:
            self.assertEqual(fh.read(), winner)

    def test_process_ladder_preserves_winner_at_install_boundary(self):
        source = {
            "kind": "jpn",
            "source_path": self.stem + ".jpn.srt",
            "source_hash": "source",
            "cues": [{"start": 0, "end": 1000, "text": "Japanese source."}],
            "tmp": False,
        }
        info = {"episodeFile": {"path": self.media}, "seriesId": 1, "title": "Show"}
        canonical = self.stem + ".ja.hi.srt"
        winner = b"1\n00:00:00,000 --> 00:00:01,000\nInstall-boundary winner.\n"
        siblings = {self.stem + ".ja.srt": b"provider",
                    self.stem + ".ja.forced.srt": b"forced"}
        for path, body in siblings.items():
            with open(path, "wb") as fh:
                fh.write(body)
        real_link = o.os.link
        def link_with_winner(src, dst):
            if dst == canonical:
                Path(canonical).write_bytes(winner)
            return real_link(src, dst)

        with patch.object(o, "translate_texts", return_value=[(0, 1, "Generated text.")]), patch.object(
            o, "upload_srt", return_value=204
        ), patch.object(o, "jellyfin_refresh"), patch.object(o, "notify_webhook"), patch.object(
            o, "registry_get", return_value=None
        ), patch.object(o, "registry_upsert"), patch.object(o, "append_state"), patch.object(
            o.os, "link", side_effect=link_with_winner
        ):
            result = o.process_ladder(self.cfg, "key", 2, "ja", "Show", "S01E01", source, info)
        self.assertEqual(result, "done")
        with open(canonical, "rb") as fh:
            self.assertEqual(fh.read(), winner)
        for path, body in siblings.items():
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), body)

    def test_process_ladder_uses_canonical_hi_target_and_registers_it(self):
        source_path = self.stem + ".jpn.srt"
        with open(source_path, "wb") as fh: fh.write(b"source")
        source = {"kind": "jpn", "source_path": source_path, "source_hash": o.file_sha256(source_path), "cues": [{"start": 0, "end": 1000, "text": "日本語"}], "tmp": False}
        info = {"episodeFile": {"path": self.media}, "seriesId": 1, "title": "Show"}
        with patch.object(o, "translate_texts", return_value=[(0, 1, "Translated")]), patch.object(o, "upload_srt", return_value=204), patch.object(o, "jellyfin_refresh"), patch.object(o, "notify_webhook"):
            self.assertEqual(o.process_ladder(self.cfg, "key", 2, "id", "Show", "S01E01", source, info), "done")
        target = self.stem + ".id.hi.srt"
        self.assertTrue(os.path.isfile(target))
        self.assertFalse(os.path.exists(self.stem + ".id.srt"))
        self.assertEqual(o.registry_get(self.stem, "id")["target_path"], target)

    def _extract(self, fn):
        streams = [{"index": 4, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "jpn"}}]
        def run(cmd, **kw):
            with open(cmd[-1], "w", encoding="utf-8") as fh: fh.write("1\n00:00:00,000 --> 00:00:01,000\n日本語\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        real_isdir = o.os.path.isdir
        with patch.object(o, "_subtitle_streams", return_value=streams), patch.object(o, "probe_audio", return_value=[]), patch.object(o.subprocess, "run", side_effect=run), patch.object(o, "map_path", side_effect=lambda p: p), patch.object(o, "load_config", return_value={"TARGET_LANGS": ["ja"]}), patch.object(o.os.path, "isdir", side_effect=lambda p: False if p == "/mnt/nas/share/media/jellyfin" else real_isdir(p)), patch.object(o.os, "walk", return_value=[(self.tmp, [], ["Show.mkv"])]):
            fn()

    def test_embedded_sweep_writes_canonical_hi_output(self):
        self._extract(lambda: o.run_embedded_srt_sweep(self.cfg, budget=1))
        self.assertTrue(os.path.exists(self.stem + ".ja.hi.srt"))
        self.assertFalse(os.path.exists(self.stem + ".ja.srt"))

    def test_webhook_extraction_writes_canonical_hi_output(self):
        self._extract(lambda: o.extract_subtitle_sidecars(self.media))
        self.assertTrue(os.path.exists(self.stem + ".ja.hi.srt"))
        self.assertFalse(os.path.exists(self.stem + ".ja.srt"))

    def test_forced_only_does_not_count_as_replaceable_target(self):
        open(self.stem + ".ja.forced.srt", "wb").close()
        self.assertIsNone(o.target_sidecar_exists(self.media, "ja"))

    def test_delete_preserves_forced_and_removes_regular_hi(self):
        for suffix in ("ja", "ja.hi", "ja.forced", "ja.hi.forced"): open(f"{self.stem}.{suffix}.srt", "wb").close()
        with patch.object(o, "get_episode", return_value={"episodeFile": {"path": self.media}}), patch.object(o, "_bazarr_wanted_refill"):
            o._delete_episode_subtitles(self.cfg, 2, langs=["ja"])
        self.assertFalse(os.path.exists(self.stem + ".ja.srt"))
        self.assertFalse(os.path.exists(self.stem + ".ja.hi.srt"))
        self.assertTrue(os.path.exists(self.stem + ".ja.forced.srt"))
        self.assertTrue(os.path.exists(self.stem + ".ja.hi.forced.srt"))

    def test_bazarr_regular_provider_landing_is_not_an_asr_output(self):
        regular = self.stem + ".jpn.srt"
        response = MagicMock(status_code=200, text="")
        response.json.return_value = {"data": [{"provider": "p", "subtitle": "s", "score": 99}]}
        def post(*args, **kwargs):
            with open(regular, "wb") as fh: fh.write(b"provider")
            return MagicMock(status_code=204, text="")
        with patch.object(o.requests, "get", return_value=response), patch.object(o.requests, "post", side_effect=post):
            result = o.bazarr_jpn_candidate(self.cfg, 2, 1, self.media, self.tmp)
        self.assertNotIn(result, (regular, self.stem + ".ja.srt"))

    def test_timeline_recalc_does_not_recreate_bare_ja_sidecar(self):
        bare = self.stem + ".ja.srt"
        with open(bare, "w") as fh: fh.write("1\n00:00:00,000 --> 00:00:01,000\nAI-generated by ASRSub\nold\n")
        row = {"stem": self.stem, "lang": "ja", "source": "asr", "source_path": bare, "source_hash": "old", "episode_id": 2}
        with patch.object(o, "load_registry", return_value={(self.stem, "ja"): row}), patch.object(o, "media_duration_s", return_value=10.0), patch.object(o, "validate_srt_timeline", side_effect=[(False, "broken"), (True, "ok")]), patch.object(o, "_registry_current_row", return_value=row), patch.object(o, "probe_audio", return_value=[]), patch.object(o, "audio_stream_signature", return_value="audio"), patch.object(o, "asr_cache_get", return_value=[{"start": 0, "end": 1000, "text": "text"}]):
            result = o.run_timeline_recalc({"TIMELINE_RECALC_BUDGET": 1})
        self.assertEqual(result["repaired"], 1)
        self.assertFalse(os.path.exists(bare))
        self.assertTrue(os.path.exists(self.stem + ".ja.hi.srt"))

    def test_provider_race_canonicalizes_new_alias_with_existing_hi(self):
        cases = ("episode", "movie")
        for kind in cases:
            with self.subTest(kind=kind):
                stem = self.stem + "." + kind
                media = stem + ".mkv"
                with open(media, "wb"):
                    pass
                canonical = stem + ".ja.hi.srt"
                new_alias = stem + ".jpn.srt"
                forced = stem + ".ja.forced.srt"
                with open(canonical, "wb") as fh:
                    fh.write(b"old-hi")
                with open(forced, "wb") as fh:
                    fh.write(b"forced")
                response = MagicMock(status_code=200, text="")
                response.json.return_value = {"data": [{"provider": "p", "subtitle": "s", "score": 99}]}
                posts = []

                def post(_url, params=None, **_kwargs):
                    posts.append(params)
                    with open(new_alias, "wb") as fh:
                        fh.write(b"new-provider")
                    return MagicMock(status_code=204, text="")

                with patch.object(o, "_BAZARR_JPN_TRIED", set()), patch.object(
                    o, "_BAZARR_JPN_MOVIE_TRIED", set()
                ), patch.object(o, "_BAZARR_JPN_CACHE", {}), patch.object(
                    o, "_BAZARR_JPN_MOVIE_CACHE", {}
                ), patch.object(o.requests, "get", return_value=response), patch.object(
                    o.requests, "post", side_effect=post
                ):
                    result = (
                        o.bazarr_jpn_candidate(self.cfg, 2, 1, media, self.tmp, return_meta=True)
                        if kind == "episode"
                        else o.bazarr_jpn_movie_candidate(self.cfg, 3, media, self.tmp, return_meta=True)
                    )
                self.assertEqual(posts[0]["hi"], "true")
                self.assertEqual(posts[0]["forced"], "false")
                self.assertEqual(result[0], canonical)
                self.assertTrue(os.path.isfile(canonical))
                self.assertFalse(os.path.exists(new_alias))
                self.assertEqual(open(forced, "rb").read(), b"forced")

    def test_embedded_registration_keeps_canonical_owned_path(self):
        canonical = self.stem + ".ja.hi.srt"
        regular = self.stem + ".ja.srt"
        with open(canonical, "wb") as fh:
            fh.write(b"canonical-embedded")
        with open(regular, "wb") as fh:
            fh.write(b"foreign-regular")
        with patch.object(o, "REGISTRY_FILE", o.REGISTRY_FILE):
            o.registry_upsert(
                self.stem, "ja", "embedded", source_path=canonical,
                source_hash=o.file_sha256(canonical), source_kind="embedded",
                audio_id="audio",
            )
            row = o.registry_get(self.stem, "ja")
            with patch.object(o, "extract_embedded_subtitle") as extract:
                o._ensure_sidecar_registered(self.stem, "ja", "audio")
            extract.assert_not_called()
            updated = o.registry_get(self.stem, "ja")
        self.assertEqual(updated["source_path"], canonical)
        self.assertEqual(updated["source_hash"], o.file_sha256(canonical))
        self.assertEqual(open(regular, "rb").read(), b"foreign-regular")

    def test_run_pass_recovers_fileless_movies_before_state_and_candidates(self):
        events = []
        cfg = {
            "TARGET_LANGS": ["ja"],
            "MAX_EPS_PER_RUN": 1,
            "MOVIE_LIBRARY": True,
            "REGEN_LIBRARY": False,
            "TMP_DIR": self.tmp,
            "TRANSLATE_API_KEY": "key",
        }
        movie = {
            "radarrId": 7,
            "sonarrEpisodeId": 7,
            "movieTitle": "Movie",
            "path": self.media,
            "movie": True,
            "missing_subtitles": [{"code2": "ja"}],
        }
        with patch.object(o, "load_config", return_value=cfg), patch.object(
            o, "consume_actions", side_effect=lambda _cfg: events.append("actions") or set()
        ), patch.object(o, "reconcile_registry", side_effect=lambda *a, **k: events.append("reconcile") or {"scanned": 0, "reconciled": 0, "invalid": 0}), patch.object(
            o, "recover_fileless_movies", side_effect=lambda *a, **k: events.append("recover") or {"cleared": {}, "skipped": 0, "fileless": 0}
        ), patch.object(o, "load_state", side_effect=lambda: events.append("state") or [{"kind": "movie", "sonarrEpisodeId": 7, "language": "ja", "status": "done"}]), patch.object(
            o, "get_wanted", side_effect=lambda _cfg: events.append("wanted") or {"total": 0, "data": []}
        ), patch.object(o, "_movie_sweep", side_effect=lambda *a, **k: events.append("candidates") or ([movie], 1)), patch.object(
            o, "_order_pass_candidates", return_value=([], 0)
        ), patch.object(o, "run_timeline_recalc", return_value={"repaired": 0, "checked": 0, "failed": []}), patch.object(
            o, "run_upgrades", return_value={"upgraded": 0, "checked": 0}
        ), patch.object(o, "run_embedded_srt_sweep", return_value={"extracted": 0, "scanned": 0, "failed": []}), patch.object(
            o, "run_jimaku_hunt", return_value={"checked": 0, "searched": 0, "landed": 0}
        ), patch.object(o, "notify_webhook"):
            o.run_pass()
        self.assertIn("recover", events)
        self.assertLess(events.index("recover"), events.index("state"))
        self.assertLess(events.index("recover"), events.index("candidates"))



    def test_run_pass_recovers_fileless_episode_target_without_touching_siblings(self):
        cfg = {"TARGET_LANGS": ["id"], "MAX_EPS_PER_RUN": 1,
               "MOVIE_LIBRARY": False, "REGEN_LIBRARY": False,
               "TMP_DIR": self.tmp, "TRANSLATE_API_KEY": "key"}
        stem = self.stem
        siblings = {stem + ".id.srt": b"provider",
                    stem + ".id.forced.srt": b"forced"}
        for path, body in siblings.items():
            with open(path, "wb") as fh:
                fh.write(body)
        canonical = stem + ".id.hi.srt"
        with open(o.STATE_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "series", "sonarrEpisodeId": 7,
                                 "language": "id", "status": "done"}) + "\n")
        with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"stem": stem, "episode_id": 7, "lang": "id",
                                 "source": "asr", "target_path": canonical,
                                 "target_hash": "stale"}) + "\n")

        ordered = []
        def capture(_cfg, wanted, movies, regen, _max_eps):
            ordered.extend(wanted + movies + regen)
            return [], 0

        with patch.object(o, "load_config", return_value=cfg), patch.object(
            o, "consume_actions", return_value=set()
        ), patch.object(o, "get_wanted", return_value={"total": 0, "data": []}), patch.object(
            o, "get_episode", return_value={"hasFile": True, "episodeFile": {"path": self.media}}
        ), patch.object(
            o, "_order_pass_candidates", side_effect=capture
        ), patch.object(o, "run_upgrades", return_value={"upgraded": 0, "checked": 0}), patch.object(
            o, "run_timeline_recalc", return_value={"repaired": 0, "checked": 0, "failed": []}
        ), patch.object(o, "run_embedded_srt_sweep", return_value={"extracted": 0, "scanned": 0, "failed": []}), patch.object(
            o, "run_jimaku_hunt", return_value={"checked": 0, "searched": 0, "landed": 0}
        ), patch.object(o, "notify_webhook"):
            o.run_pass()

        self.assertTrue(any(item.get("sonarrEpisodeId") == 7
                            and item.get("missing_subtitles") == [{"code2": "id"}]
                            for item in ordered),
                        "fileless done episode must be scheduled again")
        self.assertTrue(any(json.loads(line).get("sonarrEpisodeId") == 7
                            and json.loads(line).get("language") == "id"
                            for line in Path(o.STATE_FILE).read_text(encoding="utf-8").splitlines() if line.strip()))
        self.assertTrue(any(json.loads(line).get("episode_id") == 7
                            and json.loads(line).get("lang") == "id"
                            for line in Path(o.REGISTRY_FILE).read_text(encoding="utf-8").splitlines() if line.strip()))
        for path, body in siblings.items():
            self.assertEqual(Path(path).read_bytes(), body)
        self.assertFalse(os.path.exists(canonical))


    def test_run_pass_recovers_episode_with_malformed_jsonl_records(self):
        cfg = {"TARGET_LANGS": ["id"], "MAX_EPS_PER_RUN": 1,
               "MOVIE_LIBRARY": False, "REGEN_LIBRARY": False,
               "TMP_DIR": self.tmp, "TRANSLATE_API_KEY": "key"}
        stem = self.stem
        canonical = stem + ".id.hi.srt"
        with open(o.STATE_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(["ignored state value"]) + "\n")
            fh.write(json.dumps({"kind": "series", "sonarrEpisodeId": 7,
                                 "language": "id", "status": "done"}) + "\n")
        with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(["ignored registry value"]) + "\n")
            fh.write(json.dumps({"stem": stem, "episode_id": 7, "lang": "id",
                                 "source": "asr", "target_path": canonical,
                                 "target_hash": "stale"}) + "\n")

        ordered = []
        def capture(_cfg, wanted, movies, regen, _max_eps):
            ordered.extend(wanted + movies + regen)
            return [], 0

        with patch.object(o, "load_config", return_value=cfg), patch.object(
            o, "consume_actions", return_value=set()
        ), patch.object(o, "get_wanted", return_value={"total": 0, "data": []}), patch.object(
            o, "get_episode", return_value={"hasFile": True, "episodeFile": {"path": self.media}}
        ), patch.object(o, "_order_pass_candidates", side_effect=capture), patch.object(
            o, "run_upgrades", return_value={"upgraded": 0, "checked": 0}
        ), patch.object(o, "run_timeline_recalc", return_value={"repaired": 0, "checked": 0, "failed": []}), patch.object(
            o, "run_embedded_srt_sweep", return_value={"extracted": 0, "scanned": 0, "failed": []}
        ), patch.object(o, "run_jimaku_hunt", return_value={"checked": 0, "searched": 0, "landed": 0}), patch.object(
            o, "notify_webhook"
        ):
            o.run_pass()

        self.assertTrue(any(item.get("sonarrEpisodeId") == 7 for item in ordered),
                        "valid episode must survive malformed JSONL records")
        state_rows = [json.loads(line) for line in Path(o.STATE_FILE).read_text(encoding="utf-8").splitlines() if line.strip()]
        registry_rows = [json.loads(line) for line in Path(o.REGISTRY_FILE).read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(any(isinstance(row, dict) and row.get("sonarrEpisodeId") == 7 for row in state_rows))
        self.assertTrue(any(isinstance(row, dict) and row.get("episode_id") == 7 for row in registry_rows))

    def test_run_pass_does_not_reintroduce_excluded_fileless_episode(self):
        cfg = {"TARGET_LANGS": ["id"], "MAX_EPS_PER_RUN": 1,
               "MOVIE_LIBRARY": False, "REGEN_LIBRARY": False,
               "TMP_DIR": self.tmp, "TRANSLATE_API_KEY": "key"}
        with open(o.STATE_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "series", "sonarrEpisodeId": 7,
                                 "language": "id", "status": "done"}) + "\n")
        with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"stem": self.stem, "episode_id": 7, "lang": "id",
                                 "source": "asr", "target_path": self.stem + ".id.hi.srt",
                                 "target_hash": "stale"}) + "\n")
        with open(o.EXCLUSIONS_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"episode_id": 7}) + "\n")
        with open(o.STATE_FILE, "rb") as fh:
            state_before = fh.read()
        with open(o.REGISTRY_FILE, "rb") as fh:
            registry_before = fh.read()

        ordered = []
        def capture(_cfg, wanted, movies, regen, _max_eps):
            ordered.extend(wanted + movies + regen)
            return [], 0

        with patch.object(o, "load_config", return_value=cfg), patch.object(
            o, "consume_actions", return_value=set()
        ), patch.object(o, "get_wanted", return_value={"total": 0, "data": []}), patch.object(
            o, "get_episode", return_value={"hasFile": True, "episodeFile": {"path": self.media}}
        ), patch.object(o, "_order_pass_candidates", side_effect=capture), patch.object(
            o, "run_upgrades", return_value={"upgraded": 0, "checked": 0}
        ), patch.object(o, "run_timeline_recalc", return_value={"repaired": 0, "checked": 0, "failed": []}), patch.object(
            o, "run_embedded_srt_sweep", return_value={"extracted": 0, "scanned": 0, "failed": []}
        ), patch.object(o, "run_jimaku_hunt", return_value={"checked": 0, "searched": 0, "landed": 0}), patch.object(
            o, "notify_webhook"
        ):
            o.run_pass()

        self.assertFalse(any(item.get("sonarrEpisodeId") == 7 for item in ordered),
                         "excluded episode must not be reintroduced by recovery")
        with open(o.STATE_FILE, "rb") as fh:
            self.assertEqual(fh.read(), state_before)
        with open(o.REGISTRY_FILE, "rb") as fh:
            self.assertEqual(fh.read(), registry_before)


    def test_run_pass_recovers_from_verified_registry_stem_without_episode_lookup(self):
        cfg = {"TARGET_LANGS": ["id"], "MAX_EPS_PER_RUN": 1,
               "MOVIE_LIBRARY": False, "REGEN_LIBRARY": False,
               "TMP_DIR": self.tmp, "TRANSLATE_API_KEY": "key"}
        canonical = self.stem + ".id.hi.srt"
        siblings = {self.stem + ".id.srt": b"provider",
                    self.stem + ".id.forced.srt": b"forced"}
        for path, body in siblings.items():
            with open(path, "wb") as fh:
                fh.write(body)
        with open(o.STATE_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "series", "sonarrEpisodeId": 7,
                                 "language": "id", "status": "done"}) + "\n")
        with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"stem": self.stem, "episode_id": 7, "lang": "id",
                                 "source": "asr", "target_path": canonical,
                                 "target_hash": "stale"}) + "\n")

        ordered = []
        def capture(_cfg, wanted, movies, regen, _max_eps):
            ordered.extend(wanted + movies + regen)
            return [], 0

        with patch.object(o, "load_config", return_value=cfg), patch.object(
            o, "consume_actions", return_value=set()
        ), patch.object(o, "get_wanted", return_value={"total": 0, "data": []}), patch.object(
            o, "get_episode", side_effect=AssertionError("registry stem should avoid get_episode")
        ), patch.object(o, "_order_pass_candidates", side_effect=capture), patch.object(
            o, "run_upgrades", return_value={"upgraded": 0, "checked": 0}
        ), patch.object(o, "run_timeline_recalc", return_value={"repaired": 0, "checked": 0, "failed": []}), patch.object(
            o, "run_embedded_srt_sweep", return_value={"extracted": 0, "scanned": 0, "failed": []}
        ), patch.object(o, "run_jimaku_hunt", return_value={"checked": 0, "searched": 0, "landed": 0}), patch.object(
            o, "notify_webhook"
        ):
            o.run_pass()

        self.assertTrue(any(item.get("sonarrEpisodeId") == 7 for item in ordered),
                        "verified registry stem must recover the episode")
        self.assertTrue(any(json.loads(line).get("sonarrEpisodeId") == 7
                             for line in Path(o.STATE_FILE).read_text(encoding="utf-8").splitlines() if line.strip()))
        self.assertTrue(any(json.loads(line).get("episode_id") == 7
                             for line in Path(o.REGISTRY_FILE).read_text(encoding="utf-8").splitlines() if line.strip()))
        for path, body in siblings.items():
            self.assertEqual(Path(path).read_bytes(), body)

    def test_run_pass_bounds_fileless_episode_recovery_lookups(self):
        cfg = {"TARGET_LANGS": ["id"], "MAX_EPS_PER_RUN": 2,
               "MOVIE_LIBRARY": False, "REGEN_LIBRARY": False,
               "FILELESS_EPISODE_RECOVERY_BUDGET": 1,
               "TMP_DIR": self.tmp, "TRANSLATE_API_KEY": "key"}
        media = {}
        with open(o.STATE_FILE, "w", encoding="utf-8") as state_fh:
            for episode_id in (7, 8, 9):
                path = os.path.join(self.tmp, "Episode%d.mkv" % episode_id)
                open(path, "wb").close()
                media[episode_id] = path
                state_fh.write(json.dumps({"kind": "series", "sonarrEpisodeId": episode_id,
                                           "language": "id", "status": "done"}) + "\n")

        calls = []
        with patch.object(o, "load_config", return_value=cfg), patch.object(
            o, "consume_actions", return_value=set()
        ), patch.object(o, "get_wanted", return_value={"total": 0, "data": []}), patch.object(
            o, "get_episode", side_effect=lambda _cfg, episode_id: calls.append(episode_id) or {
                "hasFile": True, "episodeFile": {"path": media[episode_id]}
            }
        ), patch.object(o, "_order_pass_candidates", return_value=([], 0)), patch.object(
            o, "run_upgrades", return_value={"upgraded": 0, "checked": 0}
        ), patch.object(o, "run_timeline_recalc", return_value={"repaired": 0, "checked": 0, "failed": []}), patch.object(
            o, "run_embedded_srt_sweep", return_value={"extracted": 0, "scanned": 0, "failed": []}
        ), patch.object(o, "run_jimaku_hunt", return_value={"checked": 0, "searched": 0, "landed": 0}), patch.object(
            o, "notify_webhook"
        ):
            o.run_pass()

        self.assertLessEqual(len(calls), 1,
                             "fileless episode recovery must honor its configured budget")


    def test_run_pass_recovery_deletes_only_current_registry_stem(self):
        cfg = {"TARGET_LANGS": ["id"], "MAX_EPS_PER_RUN": 1,
               "MOVIE_LIBRARY": False, "REGEN_LIBRARY": False,
               "TMP_DIR": self.tmp, "TRANSLATE_API_KEY": "key"}
        canonical = self.stem + ".id.hi.srt"
        siblings = {self.stem + ".id.srt": b"provider",
                    self.stem + ".id.forced.srt": b"forced"}
        for path, body in siblings.items():
            with open(path, "wb") as fh:
                fh.write(body)
        current = {"stem": self.stem, "episode_id": 7, "lang": "id",
                   "source": "asr", "target_path": canonical,
                   "target_hash": "stale"}
        unrelated = {"stem": self.stem + ".old", "episode_id": 7,
                     "lang": "id", "source": "asr",
                     "target_path": self.stem + ".old.id.hi.srt",
                     "target_hash": "old"}
        with open(o.STATE_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "series", "sonarrEpisodeId": 7,
                                 "language": "id", "status": "done"}) + "\n")
        with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(current) + "\n")
            fh.write(json.dumps(unrelated) + "\n")

        ordered = []
        def capture(_cfg, wanted, movies, regen, _max_eps):
            ordered.extend(wanted + movies + regen)
            return [], 0

        with patch.object(o, "load_config", return_value=cfg), patch.object(
            o, "consume_actions", return_value=set()
        ), patch.object(o, "get_wanted", return_value={"total": 0, "data": []}), patch.object(
            o, "get_episode", return_value={"hasFile": True, "episodeFile": {"path": self.media}}
        ), patch.object(o, "_order_pass_candidates", side_effect=capture), patch.object(
            o, "run_upgrades", return_value={"upgraded": 0, "checked": 0}
        ), patch.object(o, "run_timeline_recalc", return_value={"repaired": 0, "checked": 0, "failed": []}), patch.object(
            o, "run_embedded_srt_sweep", return_value={"extracted": 0, "scanned": 0, "failed": []}
        ), patch.object(o, "run_jimaku_hunt", return_value={"checked": 0, "searched": 0, "landed": 0}), patch.object(
            o, "notify_webhook"
        ):
            o.run_pass()

        self.assertTrue(any(item.get("sonarrEpisodeId") == 7 for item in ordered),
                        "fileless episode must be scheduled")
        with open(o.REGISTRY_FILE, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertIn(current, rows)
        self.assertIn(unrelated, rows)
        for path, body in siblings.items():
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), body)


    def test_run_pass_recovery_respects_episode_cap_without_losing_deferred_rows(self):
        cfg = {"TARGET_LANGS": ["id"], "MAX_EPS_PER_RUN": 1,
               "MOVIE_LIBRARY": False, "REGEN_LIBRARY": False,
               "TMP_DIR": self.tmp, "TRANSLATE_API_KEY": "key"}
        media = {}
        registry_rows = []
        state_rows = []
        siblings = {}
        for episode_id in (7, 8):
            path = os.path.join(self.tmp, "Episode%d.mkv" % episode_id)
            Path(path).touch()
            media[episode_id] = path
            stem = os.path.splitext(path)[0]
            regular = stem + ".id.srt"
            forced = stem + ".id.forced.srt"
            siblings[regular] = b"provider-%d" % episode_id
            siblings[forced] = b"forced-%d" % episode_id
            state_rows.append({"kind": "series", "sonarrEpisodeId": episode_id,
                               "language": "id", "status": "done"})
            registry_rows.append({"stem": stem, "episode_id": episode_id,
                                  "lang": "id", "source": "asr",
                                  "target_path": stem + ".id.hi.srt",
                                  "target_hash": "stale"})
            for sibling, body in ((regular, siblings[regular]), (forced, siblings[forced])):
                with open(sibling, "wb") as fh:
                    fh.write(body)
        with open(o.STATE_FILE, "w", encoding="utf-8") as fh:
            for row in state_rows:
                fh.write(json.dumps(row) + "\n")
        with open(o.REGISTRY_FILE, "w", encoding="utf-8") as fh:
            for row in registry_rows:
                fh.write(json.dumps(row) + "\n")

        offered = []
        selected = []
        def cap(_cfg, wanted, movies, regen, _max_eps):
            offered.extend(wanted + movies + regen)
            selected.extend(offered[:1])
            return offered[:1], 0

        with patch.object(o, "load_config", return_value=cfg), patch.object(
            o, "consume_actions", return_value=set()
        ), patch.object(o, "get_wanted", return_value={"total": 0, "data": []}), patch.object(
            o, "get_episode", side_effect=lambda _cfg, episode_id: {
                "hasFile": True, "seasonNumber": 1, "episodeNumber": episode_id,
                "seriesTitle": "Show", "episodeFile": {"path": media[episode_id]}
            }
        ), patch.object(o, "_order_pass_candidates", side_effect=cap), patch.object(
            o, "probe_audio", return_value=[]
        ), patch.object(o, "notify_hermes"), patch.object(o, "halt_on_error"), patch.object(
            o, "append_state"
        ), patch.object(o, "run_upgrades", return_value={"upgraded": 0, "checked": 0}), patch.object(
            o, "run_timeline_recalc", return_value={"repaired": 0, "checked": 0, "failed": []}
        ), patch.object(o, "run_embedded_srt_sweep", return_value={"extracted": 0, "scanned": 0, "failed": []}), patch.object(
            o, "run_jimaku_hunt", return_value={"checked": 0, "searched": 0, "landed": 0}
        ), patch.object(o, "notify_webhook"):
            o.run_pass()

        self.assertEqual(len(offered), 2)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["sonarrEpisodeId"], 7)
        self.assertTrue(selected[0].get("recovered"))
        with open(o.STATE_FILE, encoding="utf-8") as fh:
            remaining_state = fh.read()
        with open(o.REGISTRY_FILE, encoding="utf-8") as fh:
            remaining_registry = fh.read()
        self.assertNotIn("\"sonarrEpisodeId\": 7", remaining_state)
        self.assertNotIn("\"episode_id\": 7", remaining_registry)
        self.assertIn("\"sonarrEpisodeId\": 8", remaining_state)
        self.assertIn("\"episode_id\": 8", remaining_registry)
        for path, body in siblings.items():
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), body)

    def test_normalize_late_provider_sidecars_moves_plain_jpn_to_canonical_hi(self):
        plain = Path(self.tmp) / "Show.jpn.srt"
        canonical = Path(self.tmp) / "Show.ja.hi.srt"
        body = b"1\n00:00:00,000 --> 00:00:01,000\nprovider subtitle\n"
        plain.write_bytes(body)

        o.normalize_late_provider_sidecars(self.cfg, root=self.tmp)

        self.assertFalse(plain.exists())
        with canonical.open("rb") as fh:
            self.assertEqual(fh.read(), body)

    def test_normalize_late_provider_sidecars_removes_plain_duplicate(self):
        canonical = Path(self.tmp) / "Show.ja.hi.srt"
        plain = Path(self.tmp) / "Show.ja.srt"
        forced = Path(self.tmp) / "Show.ja.forced.srt"
        canonical_body = b"1\n00:00:00,000 --> 00:00:01,000\ncanonical subtitle\n"
        plain_body = b"1\n00:00:00,000 --> 00:00:01,000\nduplicate provider\n"
        forced_body = b"1\n00:00:00,000 --> 00:00:01,000\nforced subtitle\n"
        canonical.write_bytes(canonical_body)
        plain.write_bytes(plain_body)
        forced.write_bytes(forced_body)

        o.normalize_late_provider_sidecars(self.cfg, root=self.tmp)

        self.assertFalse(plain.exists())
        with canonical.open("rb") as fh:
            self.assertEqual(fh.read(), canonical_body)
        with forced.open("rb") as fh:
            self.assertEqual(fh.read(), forced_body)

if __name__ == "__main__":
    unittest.main()
