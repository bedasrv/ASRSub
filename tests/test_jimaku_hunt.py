import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import orchestrator as o


def _write_row(path, **fields):
    """Append one raw registry row with caller-controlled timestamps"""
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


OLD = "2024-01-01T00:00:00Z"
HUNT_RESULT = {"checked": 0, "searched": 0, "landed": 0}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class JimakuHuntTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.registry = os.path.join(self.tmp, "registry.jsonl")
        self.video = os.path.join(self.tmp, "v.mkv")
        open(self.video, "w").close()
        self.cfg = {
            "TARGET_LANGS": ["ja", "id", "en"],
            "TMP_DIR": self.tmp,
            "BAZARR_URL": "http://127.0.0.1:1/api",
            "BAZARR_API_KEY": "x",
        }
        o._BAZARR_JPN_CACHE.clear()
        o._BAZARR_JPN_TRIED.clear()
        # avoid 2.5s sleeps
        self.sleep_patch = patch.object(o.time, "sleep", lambda x: None)
        self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()

    def _hunt(self, cand=None, get_episode=None, movie_candidate=None, movies=None):
        patches = [
            patch.object(o, "REGISTRY_FILE", self.registry),
            patch.object(
                o,
                "bazarr_jpn_candidate",
                side_effect=cand
                or (
                    lambda cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True: (
                        None,
                        None,
                    )
                ),
            ),
            patch.object(o, "get_movies", return_value=movies if movies is not None else {"data": [], "total": 0}),
        ]
        if movie_candidate is not None:
            patches.append(patch.object(o, "bazarr_jpn_movie_candidate", side_effect=movie_candidate))
        if get_episode is not None:
            patches.append(patch.object(o, "get_episode", side_effect=get_episode))
        for p in patches:
            p.start()
        try:
            return o.run_jimaku_hunt(self.cfg, None)
        finally:
            for p in patches:
                p.stop()


class TestBackoffSchedule(unittest.TestCase):
    def test_backoff_schedule_math(self):
        # 30m, 60m, 2h, 4h, 8h, 16h, 24h cap
        self.assertEqual(o._jimaku_hunt_backoff_minutes(1), 30)
        self.assertEqual(o._jimaku_hunt_backoff_minutes(2), 60)
        self.assertEqual(o._jimaku_hunt_backoff_minutes(3), 120)
        self.assertEqual(o._jimaku_hunt_backoff_minutes(4), 240)
        self.assertEqual(o._jimaku_hunt_backoff_minutes(5), 480)
        self.assertEqual(o._jimaku_hunt_backoff_minutes(6), 960)
        self.assertEqual(o._jimaku_hunt_backoff_minutes(7), 1440)
        self.assertEqual(o._jimaku_hunt_backoff_minutes(8), 1440)
        self.assertEqual(o._jimaku_hunt_backoff_minutes(100), 1440)
        self.assertEqual(o._jimaku_hunt_backoff_minutes(0), 0)

    def test_eligible_fresh_row_no_hunt_fields(self):
        now = datetime.now(timezone.utc)
        rec = {"stem": "/a", "lang": "ja", "source": "asr"}
        self.assertTrue(o._jimaku_hunt_eligible(rec, now))

    def test_eligible_after_backoff(self):
        now = datetime.now(timezone.utc)
        # attempt 1, last 31 min ago -> eligible (needs 30)
        rec = {"jimaku_hunt_attempts": 1, "jimaku_hunt_last_ts": _iso(now - timedelta(minutes=31))}
        self.assertTrue(o._jimaku_hunt_eligible(rec, now))
        rec2 = {"jimaku_hunt_attempts": 1, "jimaku_hunt_last_ts": _iso(now - timedelta(minutes=29))}
        self.assertFalse(o._jimaku_hunt_eligible(rec2, now))
        # attempt 2 needs 60m
        rec3 = {"jimaku_hunt_attempts": 2, "jimaku_hunt_last_ts": _iso(now - timedelta(minutes=61))}
        self.assertTrue(o._jimaku_hunt_eligible(rec3, now))
        rec4 = {"jimaku_hunt_attempts": 2, "jimaku_hunt_last_ts": _iso(now - timedelta(minutes=59))}
        self.assertFalse(o._jimaku_hunt_eligible(rec4, now))
        # cap 24h
        rec5 = {"jimaku_hunt_attempts": 10, "jimaku_hunt_last_ts": _iso(now - timedelta(minutes=1441))}
        self.assertTrue(o._jimaku_hunt_eligible(rec5, now))
        rec6 = {"jimaku_hunt_attempts": 10, "jimaku_hunt_last_ts": _iso(now - timedelta(minutes=1439))}
        self.assertFalse(o._jimaku_hunt_eligible(rec6, now))


class TestJimakuHuntEligibility(JimakuHuntTestBase):
    def test_only_asr_ja_rows_deduped_per_episode(self):
        _write_row(self.registry, stem="/t/a", lang="ja", source="asr", episode_id=1, updated_ts=OLD)
        _write_row(self.registry, stem="/t/a2", lang="ja", source="asr", episode_id=1, updated_ts="2024-06-01T00:00:00Z")
        _write_row(self.registry, stem="/t/b", lang="id", source="asr", episode_id=2, updated_ts=OLD)
        _write_row(self.registry, stem="/t/c", lang="jpn", source="jpn", episode_id=3, updated_ts=OLD)
        _write_row(self.registry, stem="/t/d", lang="en", source="eng", episode_id=4, updated_ts=OLD)
        calls = []

        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            self.assertTrue(return_meta)
            self.assertFalse(allow_existing)
            calls.append((ep_id, media_path))
            return (None, "searched")

        def fake_get_episode(cfg, ep_id):
            if ep_id == 1:
                return {"hasFile": True, "episodeFile": {"path": self.video}, "seriesId": 9}
            raise Exception("ghost")

        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand), patch.object(o, "get_episode", side_effect=fake_get_episode):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(calls, [(1, self.video)])
        self.assertEqual(res, {"checked": 1, "searched": 1, "landed": 0})

    def test_episodes_without_video_file_skipped(self):
        _write_row(self.registry, stem="/t/a", lang="ja", source="asr", episode_id=5, updated_ts=OLD)

        def fake_get_episode(cfg, ep_id):
            return {"hasFile": False, "episodeFile": {}}

        called = []

        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            called.append(ep_id)
            return (None, None)

        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand), patch.object(o, "get_episode", side_effect=fake_get_episode):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(called, [])
        self.assertEqual(res["checked"], 1)

    def test_budget_zero_disables_hunt(self):
        _write_row(self.registry, stem="/t/a", lang="ja", source="asr", episode_id=6, updated_ts=OLD)
        self.cfg["LADDER_JIMAKU_HUNT_BUDGET"] = 0
        with patch.object(o, "REGISTRY_FILE", self.registry):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(res, {"checked": 0, "searched": 0, "landed": 0})

    def test_fresh_row_eligible_immediately(self):
        # fresh row with no hunt fields should be eligible even if updated just now
        now = datetime.now(timezone.utc)
        _write_row(self.registry, stem="/t/fresh", lang="ja", source="asr", episode_id=99, updated_ts=_iso(now))
        seen = []

        def fake_get(cfg, ep_id):
            seen.append(ep_id)
            return {"hasFile": True, "episodeFile": {"path": self.video}, "seriesId": 9}

        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            return (None, "searched")

        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand), patch.object(o, "get_episode", side_effect=fake_get):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(seen, [99])
        self.assertEqual(res["checked"], 1)
        self.assertEqual(res["searched"], 1)


class TestJimakuHuntBackoffBudget(JimakuHuntTestBase):
    def test_backoff_skips_recent_miss(self):
        now = datetime.now(timezone.utc)
        recent = _iso(now - timedelta(minutes=10))
        old = _iso(now - timedelta(hours=5))
        # recent miss attempt 1 (needs 30m) -> not eligible
        _write_row(self.registry, stem="/t/recent", lang="ja", source="asr", episode_id=20, updated_ts=OLD, jimaku_hunt_last_ts=recent, jimaku_hunt_attempts=1)
        # old miss attempt 1 (31m ago, but we use 5h old -> eligible)
        _write_row(self.registry, stem="/t/old", lang="ja", source="asr", episode_id=21, updated_ts=OLD, jimaku_hunt_last_ts=old, jimaku_hunt_attempts=1)
        seen = []

        def fake_get(cfg, ep_id):
            seen.append(ep_id)
            return {"hasFile": True, "episodeFile": {"path": self.video}, "seriesId": 9}

        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=lambda *a, **kw: (None, "searched")), patch.object(o, "get_episode", side_effect=fake_get):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(seen, [21])
        self.assertEqual(res, {"checked": 1, "searched": 1, "landed": 0})

    def test_budget_caps_searches_oldest_first_by_hunt_ts(self):
        now = datetime.now(timezone.utc)
        # create 3 rows with hunt_last_ts varying; None sorts oldest
        _write_row(self.registry, stem="/t/e31", lang="ja", source="asr", episode_id=31, updated_ts=OLD, jimaku_hunt_last_ts=_iso(now - timedelta(hours=10)), jimaku_hunt_attempts=1)
        _write_row(self.registry, stem="/t/e32", lang="ja", source="asr", episode_id=32, updated_ts=OLD, jimaku_hunt_last_ts=_iso(now - timedelta(hours=5)), jimaku_hunt_attempts=1)
        _write_row(self.registry, stem="/t/e33", lang="ja", source="asr", episode_id=33, updated_ts=OLD, jimaku_hunt_last_ts=_iso(now - timedelta(hours=1)), jimaku_hunt_attempts=1)
        self.cfg["LADDER_JIMAKU_HUNT_BUDGET"] = 2
        seen = []

        def fake_get(cfg, ep_id):
            seen.append(ep_id)
            return {"hasFile": True, "episodeFile": {"path": self.video}, "seriesId": 9}

        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            self.assertFalse(allow_existing)
            return (None, "searched")

        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand), patch.object(o, "get_episode", side_effect=fake_get):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(seen, [31, 32])
        self.assertEqual(res, {"checked": 2, "searched": 2, "landed": 0})

    def test_ordering_by_last_ts_none_oldest(self):
        now = datetime.now(timezone.utc)
        _write_row(self.registry, stem="/t/fresh", lang="ja", source="asr", episode_id=40, updated_ts=OLD)
        _write_row(self.registry, stem="/t/old", lang="ja", source="asr", episode_id=41, updated_ts=OLD, jimaku_hunt_last_ts=_iso(now - timedelta(hours=1)), jimaku_hunt_attempts=2)
        self.cfg["LADDER_JIMAKU_HUNT_BUDGET"] = 1
        seen = []

        def fake_get(cfg, ep_id):
            seen.append(ep_id)
            return {"hasFile": True, "episodeFile": {"path": self.video}, "seriesId": 9}

        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            return (None, "searched")

        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand), patch.object(o, "get_episode", side_effect=fake_get):
            res = o.run_jimaku_hunt(self.cfg, None)
        # fresh (None) should be first
        self.assertEqual(seen, [40])

    def test_per_pass_budgets_respected(self):
        # shared budget 2, movie budget 1
        now = datetime.now(timezone.utc)
        for eid in [50, 51, 52]:
            _write_row(self.registry, stem=f"/t/s{eid}", lang="ja", source="asr", episode_id=eid, updated_ts=OLD)
        self.cfg["LADDER_JIMAKU_HUNT_BUDGET"] = 2
        self.cfg["LADDER_JIMAKU_HUNT_BUDGET_MOVIES"] = 1
        seen_series = []
        seen_movie = []

        def fake_get(cfg, ep_id):
            seen_series.append(ep_id)
            return {"hasFile": True, "episodeFile": {"path": self.video}, "seriesId": 9}

        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            return (None, "searched")

        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand), patch.object(o, "get_episode", side_effect=fake_get):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(len(seen_series), 2)
        self.assertEqual(res["searched"], 2)


class TestJimakuHuntMissIncrements(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.registry = os.path.join(self.tmp, "registry.jsonl")
        self.video = os.path.join(self.tmp, "v.mkv")
        open(self.video, "w").close()
        self.cfg = {"TARGET_LANGS": ["ja"], "TMP_DIR": self.tmp, "BAZARR_URL": "http://127.0.0.1:1/api", "BAZARR_API_KEY": "x"}
        o._BAZARR_JPN_CACHE.clear()
        o._BAZARR_JPN_TRIED.clear()
        self.sleep_patch = patch.object(o.time, "sleep", lambda x: None)
        self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()

    def test_miss_increments_attempts_and_sets_last_ts(self):
        _write_row(self.registry, stem=os.path.splitext(self.video)[0], lang="ja", source="asr", episode_id=70, updated_ts=OLD)
        def fake_get(cfg, ep_id):
            return {"hasFile": True, "episodeFile": {"path": self.video}, "seriesId": 9}
        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            return (None, "searched")
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand), patch.object(o, "get_episode", side_effect=fake_get):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(res["searched"], 1)
        rows = o.load_records_jsonl(self.registry)
        # last row should have hunt fields
        last = rows[-1]
        self.assertEqual(last["episode_id"], 70)
        self.assertEqual(last["jimaku_hunt_attempts"], 1)
        self.assertIn("jimaku_hunt_last_ts", last)
        # second hunt immediately should be backed off (30 min)
        o._BAZARR_JPN_TRIED.clear()
        def fake_cand2(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            self.fail("should not be called due to backoff")
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand2), patch.object(o, "get_episode", side_effect=fake_get):
            res2 = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(res2["checked"], 0)
        self.assertEqual(res2["searched"], 0)

    def test_existing_and_cache_also_increment(self):
        for meta in ("existing", "cache"):
            with self.subTest(meta=meta):
                # fresh registry each subtest
                tmp2 = tempfile.mkdtemp()
                reg2 = os.path.join(tmp2, "registry.jsonl")
                vid2 = os.path.join(tmp2, "v.mkv")
                open(vid2, "w").close()
                _write_row(reg2, stem=os.path.splitext(vid2)[0], lang="ja", source="asr", episode_id=71, updated_ts=OLD)
                def fake_get(cfg, ep_id):
                    return {"hasFile": True, "episodeFile": {"path": vid2}, "seriesId": 9}
                def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
                    return (vid2.replace(".mkv", ".jpn.srt"), meta)
                with patch.object(o, "REGISTRY_FILE", reg2), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand), patch.object(o, "get_episode", side_effect=fake_get):
                    res = o.run_jimaku_hunt(self.cfg, None)
                rows = o.load_records_jsonl(reg2)
                last = rows[-1]
                self.assertEqual(last["jimaku_hunt_attempts"], 1)

    def test_landed_download_removes_from_future_eligibility(self):
        # hunt lands download -> no hunt attempt increment, but source will change later
        # we simulate by checking that after download, hunt doesn't record attempt, and if we manually change source to jpn, future hunt ignores it
        _write_row(self.registry, stem=os.path.splitext(self.video)[0], lang="ja", source="asr", episode_id=72, updated_ts=OLD)
        def fake_get(cfg, ep_id):
            return {"hasFile": True, "episodeFile": {"path": self.video}, "seriesId": 9}
        sidecar = os.path.splitext(self.video)[0] + ".jpn.srt"
        open(sidecar, "w").close()
        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            return (sidecar, "download")
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand), patch.object(o, "get_episode", side_effect=fake_get):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(res["landed"], 1)
        # download should NOT have incremented hunt attempts (it drops out naturally)
        rows = o.load_records_jsonl(self.registry)
        # last row is still the original asr row (hunt doesn't upsert on download), so attempts should be absent/0
        asr_rows = [r for r in rows if r.get("source") == "asr" and r.get("episode_id") == 72]
        for r in asr_rows:
            self.assertIsNone(r.get("jimaku_hunt_attempts"))
        # simulate ladder converting source to jpn (as would happen after download)
        with patch.object(o, "REGISTRY_FILE", self.registry):
            o.registry_upsert(os.path.splitext(self.video)[0], "ja", "jpn", source_path=sidecar, source_hash="abc", ep_id=72, kind=None)
        # now hunt should not consider this episode (source jpn not asr)
        o._BAZARR_JPN_TRIED.clear()
        o._BAZARR_JPN_CACHE.clear()
        def fake_cand2(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            self.fail("should not be called because source changed away from asr")
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand2), patch.object(o, "get_episode", side_effect=fake_get):
            res2 = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(res2["checked"], 0)


class TestJimakuHunt429(unittest.TestCase):
    # ensure get_movies mocked
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.registry = os.path.join(self.tmp, "registry.jsonl")
        self.video = os.path.join(self.tmp, "v.mkv")
        open(self.video, "w").close()
        self.cfg = {"TARGET_LANGS": ["ja"], "TMP_DIR": self.tmp, "BAZARR_URL": "http://127.0.0.1:1/api", "BAZARR_API_KEY": "x", "LADDER_JIMAKU_HUNT_BUDGET": 5}
        o._BAZARR_JPN_CACHE.clear()
        o._BAZARR_JPN_TRIED.clear()
        o._LOG_RING.clear()
        self.sleep_patch = patch.object(o.time, "sleep", lambda x: None)
        self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()

    def test_429_aborts_remaining_budget_without_counting(self):
        now = datetime.now(timezone.utc)
        for eid in [80, 81, 82]:
            _write_row(self.registry, stem=f"/t/e{eid}", lang="ja", source="asr", episode_id=eid, updated_ts=OLD)
        def fake_get(cfg, ep_id):
            return {"hasFile": True, "episodeFile": {"path": self.video}, "seriesId": 9}
        calls = []
        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            calls.append(ep_id)
            if ep_id == 80:
                return (None, "searched")
            if ep_id == 81:
                return (None, "rate_limited")
            return (None, "searched")  # should not be called

        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand), patch.object(o, "get_episode", side_effect=fake_get):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(calls, [80, 81])
        self.assertEqual(res["searched"], 1)  # only first counted, 429 not counted
        self.assertEqual(res["landed"], 0)
        # 429 should have logged WARNING with reset_after? candidate logs it, hunt logs abort
        self.assertTrue(any("WARNING" in m and "429" in m for m in o._LOG_RING))
        # check that aborted item's attempt not counted
        rows = o.load_records_jsonl(self.registry)
        # ep 81 should not have hunt attempt recorded
        ep81_rows = [r for r in rows if r.get("episode_id") == 81 and "jimaku_hunt_attempts" in r]
        self.assertEqual(len(ep81_rows), 0)
        # ep 80 should have one
        ep80_rows = [r for r in rows if r.get("episode_id") == 80 and r.get("jimaku_hunt_attempts") == 1]
        self.assertEqual(len(ep80_rows), 1)

    def test_429_movie_aborts(self):
        # movie path
        movie_dir = tempfile.mkdtemp()
        movie_path = os.path.join(movie_dir, "m.mkv")
        open(movie_path, "w").close()
        # need registry row for movie kind
        _write_row(self.registry, stem=os.path.splitext(movie_path)[0], lang="ja", source="asr", episode_id=90, updated_ts=OLD, kind="movie")
        self.cfg["LADDER_JIMAKU_HUNT_BUDGET_MOVIES"] = 5
        calls = []
        def fake_movie_cand(cfg, radarr_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            calls.append(radarr_id)
            return (None, "rate_limited")
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [{"radarrId": 90, "path": movie_path.replace("/mnt/nas/share/media/", "/data/"), "title": "M"}]}), patch.object(o, "map_path", return_value=movie_path), patch.object(o, "bazarr_jpn_movie_candidate", side_effect=fake_movie_cand):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(calls, [90])
        self.assertEqual(res["searched"], 0)


class TestJimakuHuntFreshDownload(JimakuHuntTestBase):
    def _eligible_row(self, ep_id=41):
        _write_row(
            self.registry,
            stem=os.path.splitext(self.video)[0],
            lang="ja",
            source="asr",
            episode_id=ep_id,
            updated_ts=OLD,
        )

    def _get_episode(self, cfg, ep_id):
        return {
            "hasFile": True,
            "episodeFile": {"path": self.video},
            "seriesId": 9,
        }

    def test_fresh_download_invalidates_cache_and_counts_landed(self):
        self._eligible_row()
        o._BAZARR_JPN_CACHE[(41, 9)] = "/stale/old.jpn.srt"

        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            self.assertFalse(allow_existing)
            return (os.path.splitext(self.video)[0] + ".jpn.srt", "download")

        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand), patch.object(o, "get_episode", side_effect=self._get_episode):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(res, {"checked": 1, "searched": 1, "landed": 1})
        self.assertNotIn((41, 9), o._BAZARR_JPN_CACHE)

    def test_searched_without_sidecar_also_invalidates_cache(self):
        self._eligible_row()
        o._BAZARR_JPN_CACHE[(41, 9)] = "/stale/old.jpn.srt"
        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            self.assertFalse(allow_existing)
            return (None, "searched")

        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand), patch.object(o, "get_episode", side_effect=self._get_episode):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(res, {"checked": 1, "searched": 1, "landed": 0})
        self.assertNotIn((41, 9), o._BAZARR_JPN_CACHE)

    def test_existing_sidecar_now_triggers_search_via_allow_existing_false(self):
        self._eligible_row()
        o._BAZARR_JPN_CACHE[(41, 9)] = "/stale/old.jpn.srt"

        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            self.assertTrue(return_meta)
            self.assertFalse(allow_existing)
            return (None, "searched")

        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(o, "bazarr_jpn_candidate", side_effect=fake_cand), patch.object(o, "get_episode", side_effect=self._get_episode):
            res = o.run_jimaku_hunt(self.cfg, None)
        self.assertEqual(res, {"checked": 1, "searched": 1, "landed": 0})
        self.assertNotIn((41, 9), o._BAZARR_JPN_CACHE)


class TestJimakuHuntNeverRaises(JimakuHuntTestBase):
    def test_bazarr_http_failure_does_not_raise(self):
        _write_row(
            self.registry,
            stem=os.path.splitext(self.video)[0],
            lang="ja",
            source="asr",
            episode_id=50,
            updated_ts=OLD,
        )
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(o, "get_movies", return_value={"data": [], "total": 0}), patch.object(
            o, "get_episode", return_value={
                "hasFile": True,
                "episodeFile": {"path": self.video},
                "seriesId": 9,
            }
        ), patch.object(o.requests, "get", side_effect=Exception("boom")), patch.object(
            o.requests, "post", side_effect=Exception("boom")
        ):
            res = o.run_jimaku_hunt(self.cfg, None)  # must not raise
        self.assertEqual(res, {"checked": 1, "searched": 0, "landed": 0})


class TestBazarrJpnCandidateMeta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.video = os.path.join(self.tmp, "v.mkv")
        open(self.video, "w").close()
        self.sidecar = os.path.splitext(self.video)[0] + ".jpn.srt"
        open(self.sidecar, "w").close()
        self.cfg = {
            "BAZARR_URL": "http://b/api",
            "BAZARR_API_KEY": "k",
        }
        o._BAZARR_JPN_CACHE.clear()
        o._BAZARR_JPN_TRIED.clear()

    def _providers_get(self, candidates):
        def fake_get(url, params=None, headers=None, timeout=None):
            if "/providers/episodes" in url:
                m = MagicMock(status_code=200, text=json.dumps({"data": candidates}))
                m.json = lambda: {"data": candidates}
                return m
            m = MagicMock(status_code=200, text=json.dumps({"data": []}))
            m.json = lambda: {"data": []}
            return m
        return fake_get

    def test_default_return_unchanged_meta_true_reports_download(self):
        cand = {"provider": "jimaku", "subtitle": "opaque1", "score": 90, "language": "ja"}
        with patch.object(o.requests, "get", side_effect=self._providers_get([cand])), patch.object(
            o.requests, "post", return_value=MagicMock(status_code=204, text="")
        ):
            path = o.bazarr_jpn_candidate(self.cfg, 1, 2, self.video, None)
            o._BAZARR_JPN_TRIED.clear()
            o._BAZARR_JPN_CACHE.clear()
            with patch.object(o.requests, "get", side_effect=self._providers_get([cand])), patch.object(
                o.requests, "post", return_value=MagicMock(status_code=204, text="")
            ):
                ret = o.bazarr_jpn_candidate(self.cfg, 1, 2, self.video, None, return_meta=True)
        self.assertEqual(path, self.sidecar)
        self.assertEqual(ret, (self.sidecar, "download"))

    def test_default_true_returns_existing_without_post(self):
        existing_path = self.sidecar
        fake_data = {"data": [{"subtitles": [{"code2": "jpn", "path": existing_path.replace("/mnt/nas/share/media/", "/data/")}]}]}
        with patch.object(o, "map_path", return_value=existing_path), patch.object(
            o.requests, "get", return_value=MagicMock(status_code=200, json=lambda: fake_data, text=json.dumps(fake_data))
        ) as mock_get, patch.object(o.requests, "post") as mock_post:
            ret = o.bazarr_jpn_candidate(self.cfg, 1, 2, self.video, None, return_meta=True)
            mock_get.assert_called_once()
            mock_post.assert_not_called()
            self.assertEqual(ret, (existing_path, "existing"))
            self.assertEqual(o._BAZARR_JPN_CACHE.get((1, 2)), existing_path)

    def test_allow_existing_false_skips_cache_and_get_existing_branch(self):
        o._BAZARR_JPN_CACHE[(1, 2)] = "/stale/cached.jpn.srt"
        cand = {"provider": "jimaku", "subtitle": "opaque1", "score": 80}
        def fake_get(url, params=None, headers=None, timeout=None):
            if "/providers/episodes" in url:
                m = MagicMock(status_code=200, text=json.dumps({"data": [cand]}))
                m.json = lambda: {"data": [cand]}
                self.assertEqual(timeout, 60)
                self.assertIn("episodeid", params)
                return m
            self.fail(f"unexpected GET to {url} with allow_existing=False (existing check should be skipped)")

        with patch.object(o.requests, "get", side_effect=fake_get) as mock_get, patch.object(
            o.requests, "post", return_value=MagicMock(status_code=204, text="")
        ) as mock_post:
            ret = o.bazarr_jpn_candidate(self.cfg, 1, 2, self.video, None, return_meta=True, allow_existing=False)
            self.assertEqual(mock_get.call_count, 1)
            self.assertIn("/providers/episodes", mock_get.call_args[0][0])
            mock_post.assert_called_once()
            _, kwargs = mock_post.call_args
            self.assertEqual(kwargs["params"]["provider"], "jimaku")
            self.assertEqual(kwargs["params"]["subtitle"], "opaque1")
            self.assertEqual(kwargs["timeout"], 120)
            self.assertEqual(ret, (self.sidecar, "download"))
            self.assertEqual(o._BAZARR_JPN_CACHE.get((1, 2)), self.sidecar)
        o._BAZARR_JPN_CACHE.clear()
        o._BAZARR_JPN_TRIED.clear()
        o._BAZARR_JPN_CACHE[(3, 4)] = "/another/cached.jpn.srt"
        cand2 = {"provider": "jimaku", "subtitle": "k2", "score": 70}
        def fake_get2(url, params=None, headers=None, timeout=None):
            if "/providers/episodes" in url:
                m = MagicMock(status_code=200, text=json.dumps({"data": [cand2]}))
                m.json = lambda: {"data": [cand2]}
                return m
            self.fail("existing GET should be skipped")
        with patch.object(o.requests, "get", side_effect=fake_get2) as mock_get2, patch.object(
            o.requests, "post", return_value=MagicMock(status_code=204, text="")
        ) as mock_post2:
            ret2 = o.bazarr_jpn_candidate(self.cfg, 3, 4, self.video, None, return_meta=True, allow_existing=False)
            self.assertEqual(mock_get2.call_count, 1)
            mock_post2.assert_called_once()

    def test_429_returns_rate_limited(self):
        def fake_get(url, params=None, headers=None, timeout=None):
            if "/providers/episodes" in url:
                m = MagicMock(status_code=429, text="rate limited", headers={"x-ratelimit-reset-after": "12"})
                m.json = lambda: {"data": []}
                return m
            m = MagicMock(status_code=200, text=json.dumps({"data": []}))
            m.json = lambda: {"data": []}
            return m
        o._LOG_RING.clear()
        with patch.object(o.requests, "get", side_effect=fake_get), patch.object(o.requests, "post") as mock_post:
            ret = o.bazarr_jpn_candidate(self.cfg, 5, 6, self.video, None, return_meta=True, allow_existing=False)
            mock_post.assert_not_called()
            self.assertEqual(ret, (None, "rate_limited"))
            self.assertTrue(any("429" in msg for msg in o._LOG_RING))


class TestBazarrJpnCandidateProvidersFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.video = os.path.join(self.tmp, "v.mkv")
        open(self.video, "w").close()
        self.sidecar = os.path.splitext(self.video)[0] + ".jpn.srt"
        open(self.sidecar, "w").close()
        self.cfg = {"BAZARR_URL": "http://b/api", "BAZARR_API_KEY": "k"}
        o._BAZARR_JPN_CACHE.clear()
        o._BAZARR_JPN_TRIED.clear()
        o._LOG_RING.clear()

    def test_search_sorts_by_score_desc_and_posts_highest(self):
        low = {"provider": "jimaku", "subtitle": "low-key", "score": 30, "language": "ja"}
        high = {"provider": "jimaku", "subtitle": "high-key", "score": 95, "language": "ja"}
        def fake_get(url, params=None, headers=None, timeout=None):
            if "/providers/episodes" in url:
                self.assertEqual(params, {"episodeid": 10})
                self.assertEqual(timeout, 60)
                m = MagicMock(status_code=200, text=json.dumps({"data": [low, high]}))
                m.json = lambda: {"data": [low, high]}
                return m
            m = MagicMock(status_code=200, text=json.dumps({"data": []}))
            m.json = lambda: {"data": []}
            return m

        with patch.object(o.requests, "get", side_effect=fake_get) as mock_get, patch.object(
            o.requests, "post", return_value=MagicMock(status_code=204, text="")
        ) as mock_post:
            ret = o.bazarr_jpn_candidate(self.cfg, 10, 20, self.video, None, return_meta=True)
            mock_post.assert_called_once()
            _, kwargs = mock_post.call_args
            params = kwargs["params"]
            self.assertEqual(params["provider"], "jimaku")
            self.assertEqual(params["subtitle"], "high-key")
            self.assertEqual(params["hi"], "false")
            self.assertEqual(params["forced"], "false")
            self.assertEqual(params["original_format"], "False")
            self.assertEqual(params["seriesid"], 20)
            self.assertEqual(params["episodeid"], 10)
            self.assertEqual(kwargs["timeout"], 120)
            self.assertEqual(ret, (self.sidecar, "download"))
            self.assertTrue(any("/providers/episodes" in c[0][0] for c in mock_get.call_args_list))

    def test_no_candidates_no_post_returns_none(self):
        def fake_get(url, params=None, headers=None, timeout=None):
            if "/providers/episodes" in url:
                m = MagicMock(status_code=200, text=json.dumps({"data": []}))
                m.json = lambda: {"data": []}
                return m
            m = MagicMock(status_code=200, text=json.dumps({"data": []}))
            m.json = lambda: {"data": []}
            return m

        with patch.object(o.requests, "get", side_effect=fake_get), patch.object(
            o.requests, "post"
        ) as mock_post:
            ret = o.bazarr_jpn_candidate(self.cfg, 11, 21, self.video, None, return_meta=True)
            mock_post.assert_not_called()
            self.assertEqual(ret, (None, None))
            self.assertTrue(any("WARNING" in msg and "no candidates" in msg for msg in o._LOG_RING))

    def test_download_500_handled_no_raise(self):
        cand = {"provider": "jimaku", "subtitle": "k1", "score": 80}
        def fake_get(url, params=None, headers=None, timeout=None):
            if "/providers/episodes" in url:
                m = MagicMock(status_code=200, text=json.dumps({"data": [cand]}))
                m.json = lambda: {"data": [cand]}
                return m
            m = MagicMock(status_code=200, text=json.dumps({"data": []}))
            m.json = lambda: {"data": []}
            return m

        with patch.object(o.requests, "get", side_effect=fake_get), patch.object(
            o.requests, "post", return_value=MagicMock(status_code=500, text="internal error")
        ) as mock_post:
            ret = o.bazarr_jpn_candidate(self.cfg, 12, 22, self.video, None, return_meta=True)
            mock_post.assert_called_once()
            self.assertEqual(ret, (None, None))
            self.assertTrue(any("WARNING" in msg and "500" in msg for msg in o._LOG_RING))


class TestJimakuHuntDirectPath(JimakuHuntTestBase):
    """run_jimaku_hunt tries jimaku_direct_candidate before Bazarr for series
    items: a fresh landing counts as searched+landed and SKIPS Bazarr; any
    other meta (incl. rate_limited -> abort) follows the existing flow."""

    def _episode(self):
        return {
            "hasFile": True,
            "episodeFile": {"path": self.video},
            "seriesId": 9,
            "seriesTitle": "Jaadugar: A Witch in Mongolia",
            "seasonNumber": 1,
            "episodeNumber": 3,
        }

    def _hunt(self, direct, cand=None, cfg_extra=None):
        if cand is None:
            def cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
                return (None, "searched")
        bazarr_calls = []
        direct_calls = []

        def wrapped_direct(cfg, media_path, series_title, season, episode, tmp_dir, ep_id=None, return_meta=False, force=False):
            direct_calls.append(
                {"series_title": series_title, "season": season,
                 "episode": episode, "ep_id": ep_id,
                 "return_meta": return_meta, "force": force}
            )
            return direct(cfg, media_path, series_title, season, episode, tmp_dir,
                          ep_id=ep_id, return_meta=return_meta, force=force)

        def wrapped_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            bazarr_calls.append(ep_id)
            return cand(cfg, ep_id, series_id, media_path, tmp_dir,
                        return_meta=return_meta, allow_existing=allow_existing)

        self.cfg.update(cfg_extra or {})
        patches = [
            patch.object(o, "REGISTRY_FILE", self.registry),
            patch.object(o, "get_movies", return_value={"data": [], "total": 0}),
            patch.object(o, "jimaku_direct_candidate", side_effect=wrapped_direct),
            patch.object(o, "bazarr_jpn_candidate", side_effect=wrapped_cand),
            patch.object(o, "get_episode", return_value=self._episode()),
        ]
        for p in patches:
            p.start()
        try:
            res = o.run_jimaku_hunt(self.cfg, None)
        finally:
            for p in patches:
                p.stop()
        return res, direct_calls, bazarr_calls

    def _row(self, ep=77):
        _write_row(self.registry, stem="/t/a", lang="ja", source="asr",
                   episode_id=ep, updated_ts=OLD)

    def test_direct_hit_lands_and_skips_bazarr(self):
        self._row()
        hit = {"kind": "jpn", "source_path": self.video[:-4] + ".jpn.srt",
               "source_hash": "h" * 64, "cues": [], "duration_s": 1.0, "tmp": False}

        def direct(cfg, media_path, series_title, season, episode, tmp_dir,
                   ep_id=None, return_meta=False, force=False):
            return (hit, "hit")

        res, direct_calls, bazarr_calls = self._hunt(direct)
        self.assertEqual(bazarr_calls, [])
        self.assertEqual(res, {"checked": 1, "searched": 1, "landed": 1})
        dc = direct_calls[0]
        self.assertTrue(dc["force"] and dc["return_meta"])
        self.assertEqual(dc["series_title"], "Jaadugar: A Witch in Mongolia")
        self.assertEqual(dc["season"], 1)
        self.assertEqual(dc["episode"], 3)
        self.assertEqual(dc["ep_id"], 77)

    def test_direct_miss_falls_through_to_bazarr(self):
        self._row()

        def direct(cfg, media_path, series_title, season, episode, tmp_dir,
                   ep_id=None, return_meta=False, force=False):
            return (None, "no_entry")

        res, _, bazarr_calls = self._hunt(direct)
        self.assertEqual(bazarr_calls, [77])
        self.assertEqual(res, {"checked": 1, "searched": 1, "landed": 0})

    def test_direct_rate_limited_aborts_without_bazarr_or_attempt(self):
        self._row()
        now = datetime.now(timezone.utc)

        def direct(cfg, media_path, series_title, season, episode, tmp_dir,
                   ep_id=None, return_meta=False, force=False):
            return (None, "rate_limited")

        res, _, bazarr_calls = self._hunt(direct)
        self.assertEqual(bazarr_calls, [])
        # aborted BEFORE counting a search and WITHOUT recording an attempt
        self.assertEqual(res, {"checked": 1, "searched": 0, "landed": 0})
        rows = [json.loads(l) for l in open(self.registry)]
        row = next(r for r in rows if r.get("episode_id") == 77)
        self.assertIsNone(row.get("jimaku_hunt_last_ts"))
        self.assertIsNone(row.get("jimaku_hunt_attempts"))
        self.assertTrue(now is not None)

    def test_direct_disabled_never_called_bazarr_used(self):
        self._row()

        def direct(*a, **k):
            raise AssertionError("direct path must not run when disabled")

        res, direct_calls, bazarr_calls = self._hunt(
            direct, cfg_extra={"JIMAKU_DIRECT_ENABLED": "false"}
        )
        self.assertEqual(direct_calls, [])
        self.assertEqual(bazarr_calls, [77])
        self.assertEqual(res["searched"], 1)


if __name__ == "__main__":
    unittest.main()
