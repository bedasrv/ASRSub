import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tests import HermeticStateMixin

import orchestrator as o


class TestUploadLegacyCollision(HermeticStateMixin):
    def test_http_204_does_not_accept_different_legacy_sidecar(self):
        tmp = tempfile.mkdtemp(prefix="asrsub-upload-collision-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        media = os.path.join(tmp, "Show.mkv")
        legacy = os.path.join(tmp, "Show.jpn.srt")
        open(media, "wb").close()
        foreign = b"foreign legacy subtitle bytes"
        uploaded = b"new ASR subtitle bytes"
        with open(legacy, "wb") as fh:
            fh.write(foreign)
        response = MagicMock(status_code=204)
        response.text = ""
        cfg = {"BAZARR_URL": "http://bazarr/api", "BAZARR_API_KEY": "key"}
        result = None
        with patch.object(o.requests, "post", return_value=response):
            result = o.upload_srt(
                cfg, 4, 8, "ja", uploaded,
                filename="Show.ja.srt", media_path=media,
            )
        canonical = os.path.splitext(media)[0] + ".ja.hi.srt"
        self.assertEqual(result, 204)
        self.assertTrue(os.path.exists(canonical))
        self.assertEqual(open(legacy, "rb").read(), foreign)


if __name__ == "__main__":
    unittest.main()
