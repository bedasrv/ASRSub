import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import orchestrator as o


def _write_row(path, **fields):
    """Append one raw registry row with caller-controlled timestamps
    (default: right now -> inside the hunt cooldown)."""
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

    def _hunt(self, cand=None, get_episode=None):
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
        ]
        if get_episode is not None:
            patches.append(patch.object(o, "get_episode", side_effect=get_episode))
        for p in patches:
            p.start()
        try:
            return o.run_jimaku_hunt(self.cfg, None), patches
        finally:
            for p in patches:
                p.stop()


class TestJimakuHuntEligibility(JimakuHuntTestBase):
    def test_only_asr_ja_rows_deduped_per_episode(self):
        # eligible: ja/asr ep1 (+ a second stem for the same ep -> dedup),
        # excluded: id/asr, jpn/jpn sidecar row, en/eng
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
                return {
                    "hasFile": True,
                    "episodeFile": {"path": self.video},
                    "seriesId": 9,
                }
            raise Exception("ghost")

        res, _ = self._hunt(cand=fake_cand, get_episode=fake_get_episode)
        self.assertEqual(calls, [(1, self.video)])  # deduped; only ep 1 searched
        self.assertEqual(res, {"checked": 1, "searched": 1, "landed": 0})

    def test_episodes_without_video_file_skipped(self):
        _write_row(self.registry, stem="/t/a", lang="ja", source="asr", episode_id=5, updated_ts=OLD)

        def fake_get_episode(cfg, ep_id):
            return {"hasFile": False, "episodeFile": {}}  # no file on disk

        called = []

        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            called.append(ep_id)
            return (None, None)

        res, _ = self._hunt(cand=fake_cand, get_episode=fake_get_episode)
        self.assertEqual(called, [])
        self.assertEqual(res["checked"], 1)

    def test_budget_zero_disables_hunt(self):
        _write_row(self.registry, stem="/t/a", lang="ja", source="asr", episode_id=6, updated_ts=OLD)
        self.cfg["LADDER_JIMAKU_HUNT_BUDGET"] = 0
        res, _ = self._hunt()
        self.assertEqual(res, {"checked": 0, "searched": 0, "landed": 0})


class TestJimakuHuntCooldownBudget(JimakuHuntTestBase):
    def test_cooldown_skips_young_rows(self):
        young = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        _write_row(self.registry, stem="/t/young", lang="ja", source="asr", episode_id=20, updated_ts=young)
        _write_row(self.registry, stem="/t/old", lang="ja", source="asr", episode_id=21, updated_ts=OLD)
        seen = []

        def fake_get_episode(cfg, ep_id):
            seen.append(ep_id)
            return {
                "hasFile": True,
                "episodeFile": {"path": self.video},
                "seriesId": 9,
            }

        res, _ = self._hunt(get_episode=fake_get_episode)
        self.assertEqual(seen, [21])  # oldest-first; young row cooled down
        self.assertEqual(res, {"checked": 1, "searched": 0, "landed": 0})

    def test_budget_caps_searches_oldest_first(self):
        _write_row(self.registry, stem="/t/e33", lang="ja", source="asr", episode_id=33, updated_ts="2024-01-03T00:00:00Z")
        _write_row(self.registry, stem="/t/e32", lang="jpn", source="asr", episode_id=32, updated_ts="2024-01-02T00:00:00Z")
        _write_row(self.registry, stem="/t/e31", lang="ja", source="asr", episode_id=31, updated_ts="2024-01-01T00:00:00Z")
        self.cfg["LADDER_JIMAKU_HUNT_BUDGET"] = 2
        seen = []

        def fake_get_episode(cfg, ep_id):
            seen.append(ep_id)
            return {
                "hasFile": True,
                "episodeFile": {"path": self.video},
                "seriesId": 9,
            }

        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            self.assertFalse(allow_existing)
            return (None, "searched")  # every search consumes budget

        res, _ = self._hunt(cand=fake_cand, get_episode=fake_get_episode)
        self.assertEqual(seen, [31, 32])
        self.assertEqual(res, {"checked": 2, "searched": 2, "landed": 0})


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

        res, _ = self._hunt(cand=fake_cand, get_episode=self._get_episode)
        self.assertEqual(res, {"checked": 1, "searched": 1, "landed": 1})
        self.assertNotIn((41, 9), o._BAZARR_JPN_CACHE)

    def test_searched_without_sidecar_also_invalidates_cache(self):
        self._eligible_row()
        o._BAZARR_JPN_CACHE[(41, 9)] = "/stale/old.jpn.srt"
        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            self.assertFalse(allow_existing)
            return (None, "searched")

        res, _ = self._hunt(cand=fake_cand, get_episode=self._get_episode)
        self.assertEqual(res, {"checked": 1, "searched": 1, "landed": 0})
        self.assertNotIn((41, 9), o._BAZARR_JPN_CACHE)

    def test_existing_sidecar_now_triggers_search_via_allow_existing_false(self):
        # Hunt previously returned "existing" and kept cache; with
        # allow_existing=False it must bypass the existing-row shortcut
        # and reach the search+download POST (so searched increments and
        # stale cache is dropped).
        self._eligible_row()
        o._BAZARR_JPN_CACHE[(41, 9)] = "/stale/old.jpn.srt"

        def fake_cand(cfg, ep_id, series_id, media_path, tmp_dir, return_meta=False, allow_existing=True):
            self.assertTrue(return_meta)
            self.assertFalse(allow_existing)
            return (None, "searched")

        res, _ = self._hunt(cand=fake_cand, get_episode=self._get_episode)
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
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(
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
        """Helper: GET mock that returns existing-empty for /episodes and providers candidates."""
        def fake_get(url, params=None, headers=None, timeout=None):
            if "/providers/episodes" in url:
                m = MagicMock(status_code=200, text=json.dumps({"data": candidates}))
                m.json = lambda: {"data": candidates}
                return m
            # existing check /episodes
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
            # need fresh GET again
            with patch.object(o.requests, "get", side_effect=self._providers_get([cand])), patch.object(
                o.requests, "post", return_value=MagicMock(status_code=204, text="")
            ):
                ret = o.bazarr_jpn_candidate(self.cfg, 1, 2, self.video, None, return_meta=True)
        self.assertEqual(path, self.sidecar)
        self.assertEqual(ret, (self.sidecar, "download"))

    def test_default_true_returns_existing_without_post(self):
        # Default allow_existing=True must still return cached/existing without hitting POST
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
        # Seed cache and a GET response that would return "existing" under default;
        # with allow_existing=False both the cache lookup and the GET scan must be skipped,
        # going straight to providers search+download.
        o._BAZARR_JPN_CACHE[(1, 2)] = "/stale/cached.jpn.srt"
        cand = {"provider": "jimaku", "subtitle": "opaque1", "score": 80}
        def fake_get(url, params=None, headers=None, timeout=None):
            if "/providers/episodes" in url:
                m = MagicMock(status_code=200, text=json.dumps({"data": [cand]}))
                m.json = lambda: {"data": [cand]}
                # verify timeout 60
                self.assertEqual(timeout, 60)
                self.assertIn("episodeid", params)
                return m
            self.fail(f"unexpected GET to {url} with allow_existing=False (existing check should be skipped)")

        with patch.object(o.requests, "get", side_effect=fake_get) as mock_get, patch.object(
            o.requests, "post", return_value=MagicMock(status_code=204, text="")
        ) as mock_post:
            ret = o.bazarr_jpn_candidate(self.cfg, 1, 2, self.video, None, return_meta=True, allow_existing=False)
            # one providers GET + one POST
            self.assertEqual(mock_get.call_count, 1)
            self.assertIn("/providers/episodes", mock_get.call_args[0][0])
            mock_post.assert_called_once()
            # verify POST params
            _, kwargs = mock_post.call_args
            self.assertEqual(kwargs["params"]["provider"], "jimaku")
            self.assertEqual(kwargs["params"]["subtitle"], "opaque1")
            self.assertEqual(kwargs["timeout"], 120)
            self.assertEqual(ret, (self.sidecar, "download"))
            self.assertEqual(o._BAZARR_JPN_CACHE.get((1, 2)), self.sidecar)
        # second call with allow_existing=False should also skip cache on a fresh key
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
        # Two candidates differing in score -> highest score chosen, POST params exact
        low = {"provider": "jimaku", "subtitle": "low-key", "score": 30, "language": "ja"}
        high = {"provider": "jimaku", "subtitle": "high-key", "score": 95, "language": "ja"}
        # return low then high to verify sorting, not order
        def fake_get(url, params=None, headers=None, timeout=None):
            if "/providers/episodes" in url:
                self.assertEqual(params, {"episodeid": 10})
                self.assertEqual(timeout, 60)
                m = MagicMock(status_code=200, text=json.dumps({"data": [low, high]}))
                m.json = lambda: {"data": [low, high]}
                return m
            # existing check
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
            # also verify GET called with correct header
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
            # logged WARNING, not raised
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
            # must not raise
            ret = o.bazarr_jpn_candidate(self.cfg, 12, 22, self.video, None, return_meta=True)
            mock_post.assert_called_once()
            self.assertEqual(ret, (None, None))
            self.assertTrue(any("WARNING" in msg and "500" in msg for msg in o._LOG_RING))


class TestJimakuHuntDailyGate(unittest.TestCase):
    def setUp(self):
        self.hour = datetime.now(timezone.utc).hour
        self.today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        o._JIMAKU_HUNT_LAST_DATE = None
        o._JIMAKU_HUNT_SKIPPED_LOG_DATE = None
        o._LOG_RING.clear()

    def tearDown(self):
        o._JIMAKU_HUNT_LAST_DATE = None
        o._JIMAKU_HUNT_SKIPPED_LOG_DATE = None

    def _due_cfg(self):
        return {"JIMAKU_HUNT_HOUR": str(self.hour)}  # now >= hour: due

    def _not_due_cfg(self):
        return {"JIMAKU_HUNT_HOUR": str(self.hour + 1)}  # strictly later today

    def test_runs_once_per_calendar_day_at_or_after_hour(self):
        calls = []
        with patch.object(
            o, "run_jimaku_hunt", side_effect=lambda cfg: calls.append(cfg) or dict(HUNT_RESULT)
        ):
            first = o.run_jimaku_hunt_daily(self._due_cfg())
            second = o.run_jimaku_hunt_daily(self._due_cfg())
        self.assertEqual(len(calls), 1)
        self.assertEqual(first, HUNT_RESULT)
        self.assertIsNone(second)
        self.assertEqual(o._JIMAKU_HUNT_LAST_DATE, self.today)

    def test_before_configured_hour_skips_and_logs_once(self):
        with patch.object(o, "run_jimaku_hunt") as hunt:
            r1 = o.run_jimaku_hunt_daily(self._not_due_cfg())
            logged_after_first = [l for l in o._LOG_RING if "jimaku hunt" in l]
            r2 = o.run_jimaku_hunt_daily(self._not_due_cfg())
        self.assertIsNone(r1)
        self.assertIsNone(r2)
        self.assertEqual(len(logged_after_first), 1)  # skip logged exactly once
        hunt.assert_not_called()

    def test_already_ran_today_stays_silent(self):
        o._JIMAKU_HUNT_LAST_DATE = self.today
        ring = len(o._LOG_RING)
        with patch.object(o, "run_jimaku_hunt") as hunt:
            res = o.run_jimaku_hunt_daily(self._not_due_cfg())
        self.assertIsNone(res)
        hunt.assert_not_called()
        self.assertEqual(len(o._LOG_RING) - ring, 0)


if __name__ == "__main__":
    unittest.main()
