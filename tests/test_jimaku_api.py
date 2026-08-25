import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

import jimaku_api as ja


def _resp(status=200, payload=None, text="", headers=None):
    """Minimal response stand-in for _get_json/_raise_for_status."""
    m = MagicMock()
    m.status_code = status
    m.headers = headers or {}
    m.text = text
    if payload is None:
        m.json.side_effect = ValueError("no json")
    else:
        m.json.return_value = payload
    return m


def _stream_resp(chunks=(b"data",), status=200, headers=None):
    """Response stand-in for streaming downloads (context manager)."""
    inner = MagicMock()
    inner.status_code = status
    inner.headers = headers or {}
    inner.iter_content.return_value = list(chunks)
    ctx = MagicMock()
    ctx.__enter__.return_value = inner
    ctx.__exit__.return_value = False
    return ctx


def _client(session=None):
    return ja.JimakuClient(
        api_key="raw-key-no-bearer",
        session=session if session is not None else MagicMock(),
        call_sleep=0,
        timeout=5,
    )


class TestAuthHeader(unittest.TestCase):
    def test_raw_key_no_bearer_prefix(self):
        sess = MagicMock()
        sess.get.return_value = _resp(200, [])
        c = _client(sess)
        c.search_by_anilist(190569)
        headers = sess.get.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "raw-key-no-bearer")
        self.assertNotIn("Bearer", headers["Authorization"])

    def test_env_fallback_key(self):
        with patch.object(ja.os, "environ", {"JIMAKU_API_KEY": "env-key"}):
            c = ja.JimakuClient(call_sleep=0)
            self.assertEqual(c.api_key, "env-key")


class TestSearchParsing(unittest.TestCase):
    def test_top_level_list_parsed(self):
        entries = [{"id": 12191, "anilist_id": 190569, "name": "Jaadugar"}]
        sess = MagicMock()
        sess.get.return_value = _resp(200, entries)
        out = _client(sess).search_by_anilist(190569)
        self.assertEqual(out, entries)
        args = sess.get.call_args
        self.assertEqual(args.args[0], "https://jimaku.cc/api/entries/search")
        self.assertEqual(args.kwargs["params"], {"anilist_id": 190569})

    def test_entries_object_wrapper_tolerated(self):
        sess = MagicMock()
        sess.get.return_value = _resp(200, {"entries": [{"id": 1}]})
        self.assertEqual(_client(sess).search_by_anilist(2), [{"id": 1}])

    def test_unexpected_shape_raises_jimaku_error(self):
        sess = MagicMock()
        sess.get.return_value = _resp(200, {"oops": True})
        with self.assertRaises(ja.JimakuError):
            _client(sess).search_by_anilist(2)

    def test_non_dict_items_dropped(self):
        sess = MagicMock()
        sess.get.return_value = _resp(200, ["junk", {"id": 7}, None])
        self.assertEqual(_client(sess).search_by_anilist(2), [{"id": 7}])

    def test_http_error_raises_jimaku_error_with_snippet(self):
        sess = MagicMock()
        sess.get.return_value = _resp(500, text="boom")
        with self.assertRaises(ja.JimakuError) as cm:
            _client(sess).search_by_anilist(2)
        self.assertIn("HTTP 500", str(cm.exception))


class TestListFiles(unittest.TestCase):
    def test_episode_param_forwarded(self):
        files = [{"url": "https://jimaku.cc/entry/12191/download/a.srt", "name": "a.srt"}]
        sess = MagicMock()
        sess.get.return_value = _resp(200, files)
        out = _client(sess).list_files(12191, episode=1)
        self.assertEqual(out, files)
        args = sess.get.call_args
        self.assertEqual(args.args[0], "https://jimaku.cc/api/entries/12191/files")
        self.assertEqual(args.kwargs["params"], {"episode": 1})

    def test_episode_omitted_when_none(self):
        sess = MagicMock()
        sess.get.return_value = _resp(200, [])
        _client(sess).list_files(12191)
        self.assertIsNone(sess.get.call_args.kwargs["params"])


class TestRateLimit(unittest.TestCase):
    def test_429_raises_rate_limited_carrying_reset_after(self):
        sess = MagicMock()
        sess.get.return_value = _resp(
            429, headers={"x-ratelimit-reset-after": "42"}
        )
        with self.assertRaises(ja.JimakuRateLimited) as cm:
            _client(sess).search_by_anilist(2)
        self.assertEqual(cm.exception.reset_after, "42")

    def test_429_without_header_still_raises(self):
        sess = MagicMock()
        sess.get.return_value = _resp(429)
        with self.assertRaises(ja.JimakuRateLimited) as cm:
            _client(sess).search_by_anilist(2)
        self.assertIsNone(cm.exception.reset_after)

    def test_network_error_wrapped_as_jimaku_error(self):
        sess = MagicMock()
        sess.get.side_effect = requests.RequestException("conn refused")
        with self.assertRaises(ja.JimakuError):
            _client(sess).search_by_anilist(2)


class TestDownload(unittest.TestCase):
    def test_streams_to_dest_and_removes_part(self):
        sess = MagicMock()
        sess.get.return_value = _stream_resp([b"1\n00:", b"00:01,000 --> x"])
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "sub.srt")
            out = _client(sess).download("https://jimaku.cc/entry/1/download/sub.srt", dest)
            self.assertEqual(out, dest)
            with open(dest, "rb") as fh:
                self.assertEqual(fh.read(), b"1\n00:00:01,000 --> x")
            self.assertFalse(os.path.exists(dest + ".part"))
        headers = sess.get.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "raw-key-no-bearer")

    def test_download_429_raises_and_leaves_no_file(self):
        sess = MagicMock()
        sess.get.return_value = _stream_resp(
            status=429, headers={"x-ratelimit-reset-after": "10"}
        )
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "sub.srt")
            with self.assertRaises(ja.JimakuRateLimited):
                _client(sess).download("https://x/y.srt", dest)
            self.assertFalse(os.path.exists(dest))
            self.assertFalse(os.path.exists(dest + ".part"))

    def test_download_http_404_raises_jimaku_error(self):
        sess = MagicMock()
        sess.get.return_value = _stream_resp(status=404)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ja.JimakuError):
                _client(sess).download("https://x/y.srt", os.path.join(d, "y.srt"))


class TestPickEntry(unittest.TestCase):
    def test_exact_anilist_match_preferred(self):
        entries = [
            {"id": 1, "anilist_id": 111},
            {"id": 2, "anilist_id": 190569},
            {"id": 3, "anilist_id": 190569},
        ]
        self.assertEqual(ja.pick_entry(entries, 190569)["id"], 2)

    def test_string_anilist_ids_coerced(self):
        entries = [{"id": 5, "anilist_id": "190569"}]
        self.assertEqual(ja.pick_entry(entries, 190569)["id"], 5)

    def test_first_entry_when_no_match_or_no_id(self):
        entries = [{"id": 9}, {"id": 10}]
        self.assertEqual(ja.pick_entry(entries, 42)["id"], 9)
        self.assertEqual(ja.pick_entry(entries)["id"], 9)

    def test_empty_returns_none(self):
        self.assertIsNone(ja.pick_entry([]))
        self.assertIsNone(ja.pick_entry(None))


class TestRankFiles(unittest.TestCase):
    FILES = [
        {"name": "[VARYG] Jaadugar - S01E01.srt"},                       # release-tag srt
        {"name": "Jaadugar.S01E01.1080p.CR.WEB-DL.srt"},                 # CR web-dl srt
        {"name": "[KitaujiSub] Jaadugar Ep 1 [1080p].ass"},              # fansub ass
        {"name": "[LoliHouse] Jaadugar - 01 [WebRip 1080p].srt"},        # fansub srt
        {"name": "jaadugar_ep1.srt"},                                    # plain srt
        {"name": "[NanakoRaws] Jaadugar - 01.ass"},                      # fansub ass
        {"name": "[VARYG] Jaadugar - S01E01.CHS.srt"},                   # bilingual
        {"name": "readme.nfo"},                                          # not a sub
        {"name": "pack.zip"},
    ]

    def test_release_tag_srt_wins_over_fansub_and_plain(self):
        ranked = ja.rank_files(self.FILES)
        names = [f["name"] for f in ranked]
        self.assertEqual(names[0], "[VARYG] Jaadugar - S01E01.srt")
        self.assertEqual(
            names[1], "Jaadugar.S01E01.1080p.CR.WEB-DL.srt"
        )

    def test_plain_s01e01_srt_beats_fansub_ass(self):
        plain_ep = [{"name": "Show S01E01.srt"}, {"name": "[KitaujiSub] Show 01.ass"}]
        self.assertEqual(ja.rank_files(plain_ep)[0]["name"], "Show S01E01.srt")

    def test_fansub_files_rank_above_plain(self):
        pair = [{"name": "show_ep1.srt"}, {"name": "[LoliHouse] show - 01 [1080p].srt"}]
        ranked = ja.rank_files(pair)
        self.assertEqual(ranked[0]["name"], "[LoliHouse] show - 01 [1080p].srt")

    def test_bilingual_demoted_below_jpn_only(self):
        pair = [
            {"name": "[VARYG] Show - S01E01 CHS.srt"},
            {"name": "show_ep1_jpn_only.srt"},
        ]
        ranked = ja.rank_files(pair)
        self.assertEqual(ranked[-1]["name"], "[VARYG] Show - S01E01 CHS.srt")
        # last resort still present, never dropped
        self.assertEqual(len(ranked), 2)

    def test_non_subtitle_files_rejected(self):
        ranked = ja.rank_files([{"name": "readme.nfo"}, {"name": "pack.zip"}])
        self.assertEqual(ranked, [])

    def test_ties_keep_listing_order(self):
        pair = [
            {"name": "aaa_ep1.srt"},
            {"name": "bbb_ep1.srt"},
        ]
        ranked = ja.rank_files(pair)
        self.assertEqual([f["name"] for f in ranked], ["aaa_ep1.srt", "bbb_ep1.srt"])

    def test_ssa_accepted(self):
        ranked = ja.rank_files([{"name": "fansub ep01.ssa"}])
        self.assertEqual(ranked, [{"name": "fansub ep01.ssa"}])


class TestResolveAnilistId(unittest.TestCase):
    def setUp(self):
        fd, self.cache = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self.cache, "w") as fh:
            json.dump({}, fh)
        self.cfg = {"ANILIST_CACHE": self.cache}

    def tearDown(self):
        os.unlink(self.cache)

    def _write_cache(self, data):
        with open(self.cache, "w") as fh:
            json.dump(data, fh)

    def _read_cache(self):
        with open(self.cache) as fh:
            return json.load(fh)

    def test_cache_hit_skips_http(self):
        # real-world entry shape written by fetch_glossary.py
        self._write_cache(
            {
                "high school dxd": {
                    "fetched_at": "2026-08-23T09:48:26",
                    "media_id": 11617,
                    "title_romaji": "High School DxD",
                    "characters": [{"full": "Rias"}],
                }
            }
        )
        with patch.object(
            ja.requests, "post", side_effect=AssertionError("no HTTP on cache hit")
        ):
            self.assertEqual(ja.resolve_anilist_id(self.cfg, "High School DxD"), 11617)

    def test_cache_key_matches_fetch_glossary_normalization(self):
        # fetch_glossary.tolerant replaces ':' with a space WITHOUT
        # re-collapsing whitespace, so the shared key holds a double space;
        # pinned literally to catch any normalization drift.
        self.assertEqual(
            ja._cache_key("Jaadugar: A Witch in Mongolia"),
            "jaadugar  a witch in mongolia",
        )
        self._write_cache({"jaadugar  a witch in mongolia": {"media_id": 190569}})
        with patch.object(
            ja.requests, "post", side_effect=AssertionError("no HTTP on cache hit")
        ):
            self.assertEqual(
                ja.resolve_anilist_id(self.cfg, "Jaadugar: A Witch in Mongolia"),
                190569,
            )

    def test_miss_queries_anilist_and_writes_cache_preserving_characters(self):
        self._write_cache(
            {"jaadugar  a witch in mongolia": {"characters": [{"full": "Fine"}]}}
        )
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "data": {
                "Media": {
                    "id": 190569,
                    "title": {"romaji": "Jaadugar: A Witch in Mongolia"},
                    "format": "TV",
                }
            }
        }
        sess = MagicMock()
        sess.post.return_value = resp
        got = ja.resolve_anilist_id(
            self.cfg, "Jaadugar: A Witch in Mongolia", session=sess
        )
        self.assertEqual(got, 190569)
        url = sess.post.call_args.args[0]
        self.assertEqual(url, "https://graphql.anilist.co")
        cache = self._read_cache()
        entry = cache["jaadugar  a witch in mongolia"]
        self.assertEqual(entry["media_id"], 190569)
        self.assertEqual(entry["characters"], [{"full": "Fine"}])
        self.assertTrue(entry.get("fetched_at"))
        self.assertEqual(entry["title_romaji"], "Jaadugar: A Witch in Mongolia")

    def test_query_failure_returns_none_never_raises(self):
        sess = MagicMock()
        sess.post.side_effect = requests.RequestException("timeout")
        self.assertIsNone(ja.resolve_anilist_id(self.cfg, "Some Show", session=sess))

    def test_no_media_match_returns_none(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"data": {"Media": None}}
        sess = MagicMock()
        sess.post.return_value = resp
        self.assertIsNone(ja.resolve_anilist_id(self.cfg, "Unknown", session=sess))

    def test_empty_title_returns_none(self):
        self.assertIsNone(ja.resolve_anilist_id(self.cfg, "   "))
        self.assertIsNone(ja.resolve_anilist_id(self.cfg, None))


if __name__ == "__main__":
    unittest.main()
