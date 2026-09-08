import json
import os
import tempfile
import unittest
from unittest.mock import patch

import orchestrator as o


class TestLanguageAliasGaps(unittest.TestCase):
    def test_embedded_adoption_accepts_jp_and_enm_aliases(self):
        with tempfile.TemporaryDirectory(prefix="asrsub-alias-adoption-") as tmp:
            media = os.path.join(tmp, "Show.mkv")
            open(media, "wb").close()
            body = b"1\\n00:00:00,000 --> 00:00:01,000\\nline\\n"

            def extract(_media, _lang, output):
                with open(output, "wb") as fh:
                    fh.write(body)
                return True

            registry = os.path.join(tmp, "registry.jsonl")
            with patch.object(o, "REGISTRY_FILE", registry), patch.object(
                o, "extract_embedded_subtitle", side_effect=extract
            ), patch.object(o, "probe_audio", return_value=[]), patch.object(
                o, "audio_stream_signature", return_value="audio"
            ):
                for alias, kind in (("jp", "jpn"), ("enm", "eng")):
                    stem = os.path.join(tmp, kind)
                    media_path = stem + ".mkv"
                    open(media_path, "wb").close()
                    sidecar = stem + "." + alias + ".srt"
                    with open(sidecar, "wb") as fh:
                        fh.write(body)
                    adopted = o._adopt_embedded(stem, kind, media_path, sidecar, tmp)
                    self.assertTrue(adopted)

            with open(registry, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
            self.assertEqual([row["source_kind"] for row in rows], ["embedded", "embedded"])
            self.assertEqual([row["source_path"] for row in rows],
                             [os.path.join(tmp, "jpn.jp.srt"), os.path.join(tmp, "eng.enm.srt")])

    def test_registry_current_row_maps_jp_and_enm_source_aliases(self):
        with tempfile.TemporaryDirectory(prefix="asrsub-alias-registry-") as tmp:
            media = os.path.join(tmp, "Show.mkv")
            open(media, "wb").close()
            cases = (("jp", "Show.jp.srt"), ("enm", "Show.enm.srt"))
            for source, source_name in cases:
                source_path = os.path.join(tmp, source_name)
                target_path = os.path.join(tmp, "Show.id.srt")
                with open(source_path, "wb") as fh:
                    fh.write((source + " source").encode())
                with open(target_path, "wb") as fh:
                    fh.write(("1\n00:00:00,000 --> 00:00:01,000\n" + o.AI_MARKER + "\ntranslated target\n").encode())
                row = {
                    "stem": os.path.splitext(media)[0], "lang": "id", "source": source,
                    "source_path": source_path, "source_hash": o.file_sha256(source_path),
                    "target_path": target_path, "target_hash": o.file_sha256(target_path),
                }
                self.assertIsNotNone(o._registry_current_row(
                    row["stem"], "id", media_path=media, row=row
                ))
                with open(target_path, "wb") as fh:
                    fh.write(b"stale target")
                self.assertIsNone(o._registry_current_row(
                    row["stem"], "id", media_path=media, row=row
                ))

    def test_choose_source_aliases_match_canonical_decisions(self):
        streams = [
            {"index": 1, "tags": {"language": "jpn"}},
            {"index": 2, "tags": {"language": "eng"}},
        ]
        for alias, canonical in (("jpn", "ja"), ("enm", "en")):
            with self.subTest(alias=alias):
                self.assertEqual(
                    o.choose_source(streams, alias)["needs_translate"],
                    o.choose_source(streams, canonical)["needs_translate"],
                )


if __name__ == "__main__":
    unittest.main()
