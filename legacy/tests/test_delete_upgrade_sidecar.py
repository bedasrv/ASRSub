import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from tests import HermeticStateMixin

import orchestrator as o


def _write_row(path, **fields):
    """Append one raw registry row with caller-controlled timestamps
    (default: right now -> inside the upgrade cooldown)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {
        "stem": "",
        "lang": "",
        "episode_id": None,
        "source": "",
        "source_path": "",
        "source_hash": "",
        "created_ts": now,
        "updated_ts": now,
        "ts": now,
    }
    row.update(fields)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


MINIMAL_SRT = "1\n00:00:01,000 --> 00:00:02,000\nテスト\n\n"


class TestGlobDeletion(HermeticStateMixin):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.registry = os.path.join(self.tmp, "registry.jsonl")
        self.cfg = {
            "TARGET_LANGS": ["ja", "id", "en"],
            "TMP_DIR": self.tmp,
            "BAZARR_URL": "http://127.0.0.1:1/api",
            "BAZARR_API_KEY": "x",
        }

    def test_episode_delete_globs_hi_twins(self):
        video = os.path.join(self.tmp, "E1.mkv")
        open(video, "w").close()
        stem = os.path.splitext(video)[0]
        names = [
            "E1.ja.srt",
            "E1.ja.hi.srt",
            "E1.id.srt",
            "E1.id.hi.srt",
            "E1.id.forced.srt",
            "E1.en.srt",
        ]
        for n in names:
            open(os.path.join(self.tmp, n), "w").close()
        decoys = ["E2.ja.srt", "E1.ja.srt.bak"]
        for n in decoys:
            open(os.path.join(self.tmp, n), "w").close()
        tmp_copy = os.path.join(self.tmp, "77_ja.srt")
        open(tmp_copy, "w").close()
        with patch.object(o, "REGISTRY_FILE", self.registry):
            o.registry_upsert(stem, "ja", "asr", ep_id=77)
        refills = []
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(
            o, "get_episode", return_value={"episodeFile": {"path": video}}
        ), patch.object(
            o, "_bazarr_wanted_refill", side_effect=lambda cfg, kind: refills.append(kind)
        ):
            deleted = o._delete_episode_subtitles(self.cfg, 77)
        base = [os.path.basename(d) for d in deleted]
        for n in [n for n in names if ".forced" not in n] + ["77_ja.srt"]:
            self.assertIn(n, base, f"{n} must be deleted")
        self.assertIn("77_ja.srt", base)
        for n in names:
            if ".forced" in n:
                self.assertTrue(os.path.exists(os.path.join(self.tmp, n)), f"{n} must survive")
        for n in decoys:
            self.assertTrue(os.path.exists(os.path.join(self.tmp, n)), f"{n} must survive")

    def test_movie_delete_globs_hi_twins(self):
        video = os.path.join(self.tmp, "M1.mkv")
        open(video, "w").close()
        for n in ("M1.id.srt", "M1.id.hi.srt"):
            open(os.path.join(self.tmp, n), "w").close()
        refills = []
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(
            o, "get_movies", return_value={"data": [{"radarrId": 55, "path": video}]}
        ), patch.object(
            o, "_bazarr_wanted_refill", side_effect=lambda cfg, kind: refills.append(kind)
        ):
            deleted = o._delete_movie_subtitles(self.cfg, 55, langs=["id"])
        base = sorted(os.path.basename(d) for d in deleted)
        self.assertEqual(base, ["M1.id.hi.srt", "M1.id.srt"])
        self.assertEqual(refills, ["movie"])

    def test_episode_delete_triggers_series_wanted_refill(self):
        video = os.path.join(self.tmp, "E3.mkv")
        open(video, "w").close()
        refills = []
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(
            o, "get_episode", return_value={"episodeFile": {"path": video}}
        ), patch.object(
            o, "_bazarr_wanted_refill", side_effect=lambda cfg, kind: refills.append(kind)
        ):
            o._delete_episode_subtitles(self.cfg, 88)
        self.assertEqual(refills, ["series"])


class TestBazarrWantedRefill(HermeticStateMixin):
    def setUp(self):
        super().setUp()
        self.cfg = {
            "BAZARR_URL": "http://b1/api",
            "BAZARR_API_KEY": "k1",
            "BAZARR_URL_2": "http://b2/api",
            "BAZARR_API_KEY_2": "k2",
        }

    def test_posts_verified_task_route_to_both_instances(self):
        posts = []

        def fake_post(url, params=None, headers=None, timeout=None):
            posts.append((url, params))
            return MagicMock(status_code=204)

        with patch.object(o.requests, "post", side_effect=fake_post):
            o._bazarr_wanted_refill(self.cfg, "series")
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][0], "http://b1/api/system/tasks")
        self.assertEqual(posts[1][0], "http://b2/api/system/tasks")
        for _, params in posts:
            self.assertEqual(
                params, {"taskid": "wanted_search_missing_subtitles_series"}
            )

    def test_movie_job_id(self):
        posts = []

        def fake_post(url, params=None, headers=None, timeout=None):
            posts.append((url, params))
            return MagicMock(status_code=204)

        with patch.object(o.requests, "post", side_effect=fake_post):
            o._bazarr_wanted_refill(self.cfg, "movie")
        self.assertEqual(
            posts[0][1], {"taskid": "wanted_search_missing_subtitles_movies"}
        )

    def test_single_instance_when_secondary_unconfigured(self):
        cfg = {"BAZARR_URL": "http://b1/api", "BAZARR_API_KEY": "k1"}
        with patch.object(
            o.requests, "post", return_value=MagicMock(status_code=204)
        ) as post:
            o._bazarr_wanted_refill(cfg, "series")
        self.assertEqual(post.call_count, 1)

    def test_never_raises_on_http_error_or_exception(self):
        with patch.object(
            o.requests, "post", return_value=MagicMock(status_code=500)
        ):
            o._bazarr_wanted_refill(self.cfg, "series")
        with patch.object(o.requests, "post", side_effect=Exception("boom")):
            o._bazarr_wanted_refill(self.cfg, "series")


class TestExternalSidecarRegistration(HermeticStateMixin):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.registry = os.path.join(self.tmp, "registry.jsonl")
        self.video = os.path.join(self.tmp, "fake.mkv")
        open(self.video, "w").close()
        self.stem = os.path.splitext(self.video)[0]
        self.cand = self.stem + ".ja.srt"
        with open(self.cand, "w", encoding="utf-8") as fh:
            fh.write(MINIMAL_SRT)
        self.hash = o.file_sha256(self.cand)
        self.cfg = {"TARGET_LANGS": ["ja", "id", "en"], "TMP_DIR": self.tmp}
        self.retimed = os.path.join(self.tmp, "retimed.srt")
        with open(self.retimed, "w", encoding="utf-8") as fh:
            fh.write(MINIMAL_SRT)

    def _patches(self, trusted=False, retime_result=None):
        retimed, stats = retime_result or (self.retimed, {"method": "text", "anchors": 1, "total": 1, "matched_frac": 1.0})
        return [
            patch.object(o, "REGISTRY_FILE", self.registry),
            patch.object(
                o,
                "assess_source_file",
                return_value={
                    "ok": True,
                    "source_hash": self.hash,
                    "cues": [{"start": 1000, "end": 2000, "text": "t"}],
                },
            ),
            patch.object(o, "_sidecar_trusted", return_value=trusted),
            patch.object(o, "_adopt_embedded", return_value=False),
            patch.object(
                o, "retime_external_subtitle", return_value=(retimed, stats)
            ),
            patch.object(o, "probe_audio", return_value=[]),
            patch.object(o, "audio_stream_signature", return_value="audsig"),
        ]

    def test_accepted_retime_registers_external_row(self):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            hit = o.detect_ladder_source(self.cfg, self.video, "id", self.tmp, ep_id=42)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(hit["kind"], "jpn")
        recs = [json.loads(l) for l in open(self.registry, encoding="utf-8")]
        row = [r for r in recs if r["stem"] == self.stem and r["lang"] == "ja"]
        self.assertEqual(len(row), 1, "exactly one ja sidecar row expected")
        row = row[0]
        self.assertEqual(row["source"], "jpn")
        self.assertEqual(row["source_kind"], "external")
        self.assertEqual(row["source_hash"], self.hash)
        self.assertEqual(row["audio_id"], "audsig")
        self.assertEqual(row["episode_id"], 42)
        self.assertEqual(row["retimed"], "text")

    def test_registration_is_idempotent_per_content(self):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            o.detect_ladder_source(self.cfg, self.video, "id", self.tmp, ep_id=42)
            n_first = len(open(self.registry, encoding="utf-8").readlines())
            o.detect_ladder_source(self.cfg, self.video, "id", self.tmp, ep_id=42)
            n_second = len(open(self.registry, encoding="utf-8").readlines())
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(n_first, n_second)

    def test_rejected_retime_preserves_raw_source(self):
        patches = self._patches(
            retime_result=(None, {"method": None, "anchors": 0, "total": 1, "matched_frac": 0.0, "reason": "no reliable text anchors"})
        )
        for p in patches:
            p.start()
        try:
            hit = o.detect_ladder_source(self.cfg, self.video, "id", self.tmp, ep_id=42)
        finally:
            for p in patches:
                p.stop()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["kind"], "jpn")
        self.assertEqual(hit["cues"][0]["start"], 1000)
        self.assertTrue(os.path.exists(self.registry), "raw fallback must be registered")
        with patch.object(o, "REGISTRY_FILE", self.registry):
            row = o.registry_get(self.stem, "jpn")
        self.assertEqual(row["timeline_kind"], "raw")
        self.assertEqual(row["retime_status"], "rejected")

    def test_trusted_external_file_skips_retime_gate(self):
        with patch.object(o, "REGISTRY_FILE", self.registry):
            o.registry_upsert(
                self.stem,
                "jpn",
                "jpn",
                source_path=self.cand,
                source_hash=self.hash,
                source_kind="external",
                ep_id=42,
                audio_id="audsig",
            )
        calls = {"retime": 0}

        def bomb(*a, **kw):
            calls["retime"] += 1
            raise AssertionError("retime gate must be skipped for trusted external files")

        patches = [
            patch.object(o, "REGISTRY_FILE", self.registry),
            patch.object(
                o,
                "assess_source_file",
                return_value={
                    "ok": True,
                    "source_hash": self.hash,
                    "cues": [{"start": 1000, "end": 2000, "text": "t"}],
                },
            ),
            patch.object(o, "retime_external_subtitle", side_effect=bomb),
            patch.object(o, "probe_audio", return_value=[]),
            patch.object(o, "audio_stream_signature", return_value="audsig"),
        ]
        for p in patches:
            p.start()
        try:
            hit = o.detect_ladder_source(self.cfg, self.video, "id", self.tmp, ep_id=42)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(hit["kind"], "jpn")
        self.assertNotIn("align_tmp", hit)
        self.assertEqual(calls["retime"], 0)


class TestSidecarTrustedKinds(HermeticStateMixin):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.registry = os.path.join(self.tmp, "registry.jsonl")
        self.video = os.path.join(self.tmp, "v.mkv")
        open(self.video, "w").close()
        self.stem = os.path.splitext(self.video)[0]
        self.cand = self.stem + ".jpn.srt"
        with open(self.cand, "w", encoding="utf-8") as fh:
            fh.write(MINIMAL_SRT)
        self.hash = o.file_sha256(self.cand)

    def _row(self, kind, audio="audsig", hash_=None):
        with patch.object(o, "REGISTRY_FILE", self.registry):
            o.registry_upsert(
                self.stem,
                "jpn",
                "jpn",
                source_path=self.cand,
                source_hash=hash_ if hash_ is not None else self.hash,
                source_kind=kind,
                audio_id=audio,
            )

    def _trusted(self, sig="audsig"):
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(
            o, "probe_audio", return_value=[]
        ), patch.object(o, "audio_stream_signature", return_value=sig):
            return o._sidecar_trusted(self.stem, "jpn", self.video, self.cand)

    def test_external_row_with_matching_hash_and_audio_is_trusted(self):
        self._row("external")
        self.assertTrue(self._trusted())

    def test_clobbered_hash_not_trusted(self):
        with open(self.cand, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:09,000 --> 00:00:10,000\nclobbered\n\n")
        self._row("external")
        self.assertFalse(self._trusted())

    def test_stale_audio_not_trusted(self):
        self._row("external")
        self.assertFalse(self._trusted(sig="othersig"))

    def test_unknown_kind_not_trusted(self):
        self._row("mystery")
        self.assertFalse(self._trusted())

    def test_missing_row_not_trusted(self):
        self.assertFalse(self._trusted())


class TestUpgradeEligibility(HermeticStateMixin):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.registry = os.path.join(self.tmp, "registry.jsonl")
        self.refined = os.path.join(self.tmp, "refine_state.jsonl")
        self.cfg = {
            "TARGET_LANGS": ["ja", "id", "en"],
            "LADDER_UPGRADE_BUDGET": 4,
            "LADDER_COOLDOWN_H": 24,
        }
        self.old = "2026-01-01T00:00:00Z"

    def _run(self):
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(
            o, "REFINE_STATE_FILE", self.refined
        ):
            return o.run_upgrades(self.cfg, None)

    def test_jpn_output_rows_eligible_sidecar_rows_excluded(self):
        _write_row(self.registry, stem="/t/a", lang="ja", source="jpn", episode_id=1, updated_ts=self.old)
        _write_row(self.registry, stem="/t/b", lang="id", source="asr", episode_id=2, updated_ts="2026-01-02T00:00:00Z")
        _write_row(self.registry, stem="/t/c", lang="jpn", source="jpn", episode_id=3, updated_ts="2026-01-03T00:00:00Z")
        _write_row(self.registry, stem="/t/d", lang="en", source="eng", episode_id=4)
        seen = []

        def fake_get_episode(cfg, ep_id):
            seen.append(ep_id)
            raise Exception("sonarr down")

        with patch.object(o, "get_episode", side_effect=fake_get_episode):
            res = self._run()
        self.assertEqual(res, {"upgraded": 0, "checked": 2})
        self.assertEqual(seen, [1, 2])  # oldest-first; lang=jpn sidecar row excluded

    def test_unchanged_jpn_source_hash_skips_retranslation(self):
        media = "/tmp/upg_fake.mkv"
        stem = os.path.splitext(media)[0]
        open(media, "w").close()
        _write_row(self.registry, stem=stem, lang="ja", source="jpn", source_hash="AAA", episode_id=7, updated_ts=self.old,
                   effective_timeline=[[0, 500]], effective_timeline_hash=o._timeline_hash([[0, 500]]),
                   retime_policy_version=o.RETIME_POLICY_VERSION, timeline_kind="raw")
        ladder = {
            "kind": "jpn",
            "source_path": stem + ".ja.srt",
            "source_hash": "AAA",
            "cues": [{"start": 0, "end": 500, "text": "x"}],
            "effective_timeline": [[0, 500]],
            "effective_timeline_hash": o._timeline_hash([[0, 500]]),
            "retime_policy_version": o.RETIME_POLICY_VERSION,
            "timeline_kind": "raw",
            "duration_s": None,
            "tmp": False,
        }
        with patch.object(
            o,
            "get_episode",
            return_value={
                "hasFile": True,
                "episodeFile": {"path": media},
                "seriesId": 5,
                "title": "S",
            },
        ), patch.object(o, "detect_ladder_source", return_value=ladder), patch.object(
            o, "process_ladder", return_value="done"
        ) as pl:
            res = self._run()
        self.assertEqual(res, {"upgraded": 0, "checked": 1})
        pl.assert_not_called()

    def test_changed_jpn_source_triggers_upgrade(self):
        media = "/tmp/upg_fake.mkv"
        stem = os.path.splitext(media)[0]
        open(media, "w").close()
        with open(stem + ".ja.srt", "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\nAI-generated by ASRSub\nsource\n")
        _write_row(self.registry, stem=stem, lang="ja", source="asr", source_hash="OLD", episode_id=7, updated_ts=self.old)
        ladder = {
            "kind": "jpn",
            "source_path": stem + ".ja.srt",
            "source_hash": "NEW",
            "cues": [{"start": 0, "end": 500, "text": "x"}],
            "duration_s": None,
            "tmp": False,
        }
        with patch.object(
            o,
            "get_episode",
            return_value={
                "hasFile": True,
                "episodeFile": {"path": media},
                "seriesId": 5,
                "title": "S",
            },
        ), patch.object(o, "detect_ladder_source", return_value=ladder), patch.object(
            o, "process_ladder", return_value="done"
        ) as pl:
            res = self._run()
        self.assertEqual(res["upgraded"], 1)
        self.assertEqual(pl.call_count, 1)


if __name__ == "__main__":
    unittest.main()
