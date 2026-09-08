import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests import HermeticStateMixin

import orchestrator as o


class TestEmbeddedTargetLanguages(HermeticStateMixin):
    def test_webhook_extracts_configured_targets_with_canonical_stream_codes(self):
        self.tmp = tempfile.mkdtemp()
        media = os.path.join(self.tmp, "Episode.mkv")
        open(media, "wb").close()
        streams = [
            {"index": 3, "codec_type": "subtitle", "codec_name": "ass",
             "tags": {"language": "jpn", "NUMBER_OF_FRAMES": "300"}},
            {"index": 4, "codec_type": "subtitle", "codec_name": "ssa",
             "tags": {"language": "ind", "NUMBER_OF_FRAMES": "200"}},
            {"index": 5, "codec_type": "subtitle", "codec_name": "subrip",
             "tags": {"language": "eng", "NUMBER_OF_FRAMES": "100"}},
        ]
        ffmpeg_maps = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffmpeg":
                ffmpeg_maps.append(cmd[cmd.index("-map") + 1])
                with open(cmd[-1], "w", encoding="utf-8") as fh:
                    fh.write("1\n00:00:00,000 --> 00:00:01,000\nline\n")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise AssertionError(f"unexpected subprocess: {cmd[0]}")

        with patch.object(o, "REGISTRY_FILE", os.path.join(self.tmp, "registry.jsonl")), \
             patch.object(o, "load_config", return_value={"TARGET_LANGS": ["ja", "id", "en"]}), \
             patch.object(o, "probe_audio", return_value=[]), \
             patch.object(o, "_subtitle_streams", return_value=streams), \
             patch.object(o.subprocess, "run", side_effect=fake_run), \
             patch.object(o, "map_path", side_effect=lambda path: path):
            o.extract_subtitle_sidecars(media)

        stem = os.path.splitext(media)[0]
        self.assertEqual(ffmpeg_maps, ["0:3", "0:4", "0:5"])
        for lang in ("ja", "id", "en"):
            self.assertTrue(os.path.isfile(f"{stem}.{lang}.hi.srt"))
        with patch.object(o, "REGISTRY_FILE", os.path.join(self.tmp, "registry.jsonl")):
            for lang in ("ja", "id", "en"):
                self.assertIsNotNone(o.registry_get(stem, lang))

    def test_picker_accepts_two_letter_and_iso6393_indonesian_codes(self):
        for metadata_code in ("id", "ind"):
            with self.subTest(metadata_code=metadata_code):
                stream = {
                    "index": 7,
                    "codec_type": "subtitle",
                    "codec_name": "ass",
                    "tags": {"language": metadata_code},
                }
                self.assertEqual(o._pick_best_subtitle([stream], "id"), stream)


    def test_webhook_retains_explicit_unknown_target_without_defaulting(self):
        self.tmp = tempfile.mkdtemp()
        media = os.path.join(self.tmp, "Episode.mkv")
        open(media, "wb").close()
        streams = [{"index": 3, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "jpn"}}, {"index": 4, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "fr"}}, {"index": 5, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "eng"}}]
        ffmpeg_maps = []
        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffmpeg":
                ffmpeg_maps.append(cmd[cmd.index("-map") + 1])
                with open(cmd[-1], "w", encoding="utf-8") as fh:
                    fh.write("1\n00:00:00,000 --> 00:00:01,000\nline\n")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise AssertionError(f"unexpected subprocess: {cmd[0]}")
        with patch.object(o, "load_config", return_value={"TARGET_LANGS": ["fr"]}), patch.object(o, "probe_audio", return_value=[]), patch.object(o, "_subtitle_streams", return_value=streams), patch.object(o.subprocess, "run", side_effect=fake_run), patch.object(o, "map_path", side_effect=lambda path: path):
            o.extract_subtitle_sidecars(media)
        self.assertEqual(ffmpeg_maps, ["0:4"])
        self.assertTrue(os.path.isfile(os.path.splitext(media)[0] + ".fr.hi.srt"))

    def test_missing_embedded_target_does_not_adopt_external_sidecar(self):
        self.tmp = tempfile.mkdtemp()
        media = os.path.join(self.tmp, "Episode.mkv")
        open(media, "wb").close()
        sidecar = os.path.splitext(media)[0] + ".id.srt"
        with open(sidecar, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\nexternal\n")
        streams = [{"index": 3, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "jpn"}}]
        with patch.object(o, "load_config", return_value={"TARGET_LANGS": ["id"]}), patch.object(o, "probe_audio", return_value=[]), patch.object(o, "_subtitle_streams", return_value=streams), patch.object(o, "map_path", side_effect=lambda path: path):
            o.extract_subtitle_sidecars(media)
        self.assertIsNone(o.registry_get(os.path.splitext(media)[0], "id"))
        o.registry_upsert(os.path.splitext(media)[0], "id", "external", source_kind="external", source_path=sidecar)
        o.extract_subtitle_sidecars(media)
        self.assertEqual(o.registry_get(os.path.splitext(media)[0], "id").get("source_kind"), "external")
        with open(sidecar, encoding="utf-8") as fh:
            self.assertIn("external", fh.read())

    def test_stale_embedded_row_is_not_refreshed_without_matching_audio(self):
        self.tmp = tempfile.mkdtemp()
        stem = os.path.join(self.tmp, "Episode")
        sidecar = stem + ".id.srt"
        with open(sidecar, "w", encoding="utf-8") as fh:
            fh.write("old sidecar\n")
        old_hash = o.file_sha256(sidecar)
        o.registry_upsert(stem, "id", "embedded", source_path=sidecar, source_hash=old_hash, source_kind="embedded", audio_id="old-audio")
        o._ensure_sidecar_registered(stem, "id", "new-audio")
        row = o.registry_get(stem, "id")
        self.assertEqual(row.get("audio_id"), "old-audio")
        self.assertEqual(row.get("source_hash"), old_hash)
        with open(sidecar, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "old sidecar\n")

if __name__ == "__main__":
    unittest.main()
