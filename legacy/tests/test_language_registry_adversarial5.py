import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests import HermeticStateMixin

import control_api_v2 as api2
import orchestrator as o


class TestLanguageRegistryAdversarial5(HermeticStateMixin):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="asrsub-adversarial5-")
        self.registry = os.path.join(self.tmp, "registry.jsonl")

    def test_canonical_preledger_sidecar_can_be_adopted(self):
        media = os.path.join(self.tmp, "Show.mkv")
        sidecar = os.path.join(self.tmp, "Show.ja.srt")
        extracted = os.path.join(self.tmp, "extracted.srt")
        open(media, "wb").close()
        body = b"1\n00:00:00,000 --> 00:00:01,000\nJapanese\n"
        with open(sidecar, "wb") as fh:
            fh.write(body)
        def extract(_media, _lang, out):
            with open(out, "wb") as fh:
                fh.write(body)
            return True
        with patch.object(o, "REGISTRY_FILE", self.registry), patch.object(
            o, "extract_embedded_subtitle", side_effect=extract
        ), patch.object(o, "probe_audio", return_value=[]), patch.object(
            o, "audio_stream_signature", return_value="audio"
        ):
            adopted = o._adopt_embedded(
                os.path.splitext(media)[0], "jpn", media, sidecar, self.tmp
            )
        self.assertTrue(adopted)
        rows = [json.loads(line) for line in open(self.registry, encoding="utf-8")]
        self.assertEqual(rows[-1]["lang"], "ja")
        self.assertEqual(rows[-1]["source_kind"], "embedded")
        self.assertEqual(rows[-1]["source_path"], sidecar)
        self.assertEqual(rows[-1]["source_hash"], o.file_sha256(sidecar))

    def test_upload_sidecar_verification_recognizes_existing_legacy_alias(self):
        media = os.path.join(self.tmp, "Show.mkv")
        legacy = os.path.join(self.tmp, "Show.jpn.srt")
        open(media, "wb").close()
        body = b"legacy subtitle"
        with open(legacy, "wb") as fh:
            fh.write(body)
        cfg = {"BAZARR_URL": "unused", "BAZARR_API_KEY": "key"}
        path, wrote = o._ensure_sidecar_on_disk(cfg, media, "ja", body, retries=1, delay=0)
        self.assertEqual(path, os.path.join(self.tmp, "Show.ja.hi.srt"))
        self.assertTrue(wrote)
        self.assertTrue(os.path.exists(os.path.splitext(media)[0] + ".ja.hi.srt"))
        self.assertEqual(open(legacy, "rb").read(), body)

    def test_wanted_response_normalizes_bazarr_missing_aliases(self):
        env = os.path.join(self.tmp, "pipeline.env")
        with open(env, "w", encoding="utf-8") as fh:
            fh.write("TARGET_LANGS=ja,id,en\n")
        api = api2.ControlAPIv2({
            "ENV_FILE": env,
            "OVERRIDE_FILE": os.path.join(self.tmp, "overrides.json"),
            "STATE_FILE": os.path.join(self.tmp, "state.jsonl"),
            "REGISTRY_FILE": self.registry,
        })
        wanted = {"total": 1, "data": [{
            "sonarrEpisodeId": 11,
            "seriesTitle": "Show",
            "episodeTitle": "Episode",
            "missing_subtitles": [
                {"code2": "jpn"}, {"code2": "ind"}, {"code2": "enm"}
            ],
        }]}
        with patch.object(api, "_bazarr_wanted", return_value=wanted), patch.object(
            api, "_ep_detail", return_value={}
        ), patch.object(api, "_daemon_status", return_value={"reachable": False}), patch.object(
            api, "_subs_cached", return_value=[]
        ):
            code, response = api._h_wanted(None)
        self.assertEqual(code, 200)
        self.assertEqual(set(response["items"][0]["missing"]), {"ja", "id", "en"})


if __name__ == "__main__":
    unittest.main()
