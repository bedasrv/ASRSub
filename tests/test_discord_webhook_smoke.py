import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test_discord_webhook.py"


spec = importlib.util.spec_from_file_location("discord_webhook_smoke", SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load webhook smoke script")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


VALID_WEBHOOK = b"https://discord.com/api/webhooks/12345678901234567/a_b-C.1"


class _CaptureHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        self.__class__.requests.append(
            {
                "path": self.path,
                "body": self.rfile.read(length),
                "content_type": self.headers.get("Content-Type"),
            }
        )
        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *_args):
        return


class TestDiscordWebhookSmoke(unittest.TestCase):
    def test_default_secret_path_is_home_scoped(self):
        self.assertEqual(
            smoke.default_secret_path(),
            Path.home() / ".config" / "asr-pipeline" / "secrets" / "discord_webhook",
        )

    def test_payload_disables_mentions_and_has_no_secret(self):
        payload = smoke.build_payload()
        decoded = json.loads(payload)
        self.assertEqual(decoded["allowed_mentions"], {"parse": []})
        self.assertIn("ASRSub webhook smoke test", decoded["content"])
        self.assertNotIn(VALID_WEBHOOK.decode(), payload.decode())

    def test_secret_reader_requires_private_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            path = root / "discord_webhook"
            path.write_bytes(VALID_WEBHOOK)
            os.chmod(path, 0o600)
            self.assertEqual(smoke.read_secret(path), VALID_WEBHOOK)
            os.chmod(path, 0o644)
            with self.assertRaises(smoke.SmokeError):
                smoke.read_secret(path)

    def test_post_uses_local_http_server_and_returns_status_only(self):
        _CaptureHandler.requests = []
        server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/webhook"
            status = smoke.post_payload(url, smoke.build_payload())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(status, 204)
        self.assertEqual(len(_CaptureHandler.requests), 1)
        request = _CaptureHandler.requests[0]
        self.assertEqual(request["path"], "/webhook")
        self.assertEqual(request["content_type"], "application/json")
        self.assertEqual(json.loads(request["body"])["allowed_mentions"], {"parse": []})


if __name__ == "__main__":
    unittest.main()
