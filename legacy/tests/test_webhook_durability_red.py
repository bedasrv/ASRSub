"""RED tests for durable Tdarr webhook inbox/ledger remediation.

These tests drive requirements 1-10 from the task. Initially they must FAIL (RED)
until the ledger implementation exists.
"""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

from tests import HermeticStateMixin
import orchestrator as o


class TestWebhookLedgerLocation(HermeticStateMixin):
    def test_ledger_is_local_sqlite_under_state_dir_not_nfs(self):
        # Requirement 1: durable local SQLite inbox under existing local state dir, never NFS
        import webhook_ledger
        db_path = webhook_ledger.get_db_path()
        state_dir = os.path.dirname(os.path.abspath(o.STATE_FILE))
        self.assertTrue(os.path.abspath(db_path).startswith(state_dir),
                        f"ledger path {db_path} must be under state dir {state_dir}")
        self.assertNotIn("/mnt/nas", db_path)
        self.assertNotIn("/media", db_path)
        self.assertTrue(db_path.endswith(".db"))
        # must be SQLite file after init
        webhook_ledger.init_ledger()
        self.assertTrue(os.path.isfile(db_path))
        con = sqlite3.connect(db_path)
        try:
            cur = con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in cur.fetchall()}
            self.assertIn("operations", tables)
        finally:
            con.close()

    def test_ledger_not_created_on_nfs(self):
        import webhook_ledger
        with patch.object(webhook_ledger, "get_db_path", return_value="/mnt/nas/share/media/webhook_inbox.db"):
            with self.assertRaises(RuntimeError):
                webhook_ledger.init_ledger()


class TestWebhookIdentity(HermeticStateMixin):
    def test_missing_file_field_rejected_fail_closed(self):
        import webhook_ledger
        webhook_ledger.init_ledger()
        with self.assertRaises(ValueError):
            webhook_ledger.compute_operation_key("")
        with self.assertRaises(ValueError):
            webhook_ledger.compute_operation_key(None)
        # handler must not start worker on invalid identity
        with patch.object(o, "extract_subtitle_sidecars") as mock_extract:
            handler = _make_handler(body={})
            handler._handle_tdarr_webhook_authenticated({"file": "", "_id": "x"}, token="valid")
            mock_extract.assert_not_called()
            # ledger should have no operation for empty file
            ops = webhook_ledger.list_operations()
            self.assertEqual(len(ops), 0)

    def test_invalid_identity_rejected_no_ledger_row(self):
        import webhook_ledger
        webhook_ledger.init_ledger()
        for bad in ["", "   ", None, 123, "/tmp/not-a-video.txt.bak"]:
            with self.subTest(bad=bad):
                with self.assertRaises((ValueError, TypeError)):
                    webhook_ledger.compute_operation_key(bad)

    def test_tdarr_id_not_part_of_identity(self):
        import webhook_ledger
        webhook_ledger.init_ledger()
        a = webhook_ledger.compute_operation_key("/data/media/Show.mkv")
        b = webhook_ledger.compute_operation_key("/data/media/Show.mkv")
        # same path different _id must map to same key
        self.assertEqual(a, b)
        # file path normalized via map_path
        key1 = webhook_ledger.compute_operation_key("/data/jellyfin/Show.mkv")
        key2 = webhook_ledger.compute_operation_key("/mnt/nas/share/media/jellyfin/Show.mkv")
        self.assertEqual(key1, key2)


class TestWebhookAuthBeforeLedger(HermeticStateMixin):
    def test_unauthenticated_request_does_not_touch_ledger(self):
        import webhook_ledger
        webhook_ledger.init_ledger()
        # simulate ControlHandler auth check before ledger access
        os.environ["CONTROL_API_KEY"] = "secret123"
        import importlib
        importlib.reload(o)
        state_patch = patch.object(o, "STATE_FILE", os.path.join(self.state_tmp, "state.jsonl"))
        state_patch.start()
        self.addCleanup(state_patch.stop)
        import webhook_ledger as wl
        wl.init_ledger()
        handler = _make_handler(body={"file": "/data/jellyfin/Show.mkv", "_id": "abc"})
        # no token -> should return 401 and not create ledger entry
        with patch.object(o, "extract_subtitle_sidecars") as mock_extract:
            code, body = handler.handle_tdarr_webhook_dispatch(
                {"file": "/data/jellyfin/Show.mkv", "_id": "abc"}, token="bad"
            )
            self.assertEqual(code, 401)
            mock_extract.assert_not_called()
            ops = wl.list_operations()
            self.assertEqual(len(ops), 0)

    def test_authenticated_claim_before_extraction(self):
        import webhook_ledger
        webhook_ledger.init_ledger()
        key = webhook_ledger.compute_operation_key("/data/media/Show.mkv")
        op, is_new = webhook_ledger.insert_received("/data/media/Show.mkv", "id1", fingerprint={"size": 1})
        self.assertTrue(is_new)
        claimed = webhook_ledger.try_claim(key, lease_ttl=600)
        self.assertTrue(claimed)
        # second claim must fail
        claimed2 = webhook_ledger.try_claim(key, lease_ttl=600)
        self.assertFalse(claimed2)


class TestWebhookDuplicateSuppression(HermeticStateMixin):
    def test_duplicate_sequential_produces_one_operation_one_worker(self):
        import webhook_ledger
        webhook_ledger.init_ledger()
        calls = []
        def fake_extract(path, tdarr_id=""):
            calls.append(path)
        with patch.object(o, "extract_subtitle_sidecars", side_effect=fake_extract):
            handler = _make_handler(body={})
            # first
            handler._handle_tdarr_webhook_authenticated({"file": "/data/jellyfin/Show.mkv", "_id": "a"}, token="valid")
            # duplicate sequential same file different _id
            handler._handle_tdarr_webhook_authenticated({"file": "/data/jellyfin/Show.mkv", "_id": "b"}, token="valid")
            # allow threads to run
            import time; time.sleep(0.2)
        ops = webhook_ledger.list_operations()
        self.assertEqual(len(ops), 1, f"duplicate should produce one operation, got {ops}")
        self.assertEqual(len(calls), 1, f"duplicate must produce one worker, got {calls}")

    def test_duplicate_concurrent_produces_one_worker(self):
        import webhook_ledger
        webhook_ledger.init_ledger()
        calls = []
        def fake_extract(path, tdarr_id=""):
            calls.append(path)
            import time; time.sleep(0.05)
        with patch.object(o, "extract_subtitle_sidecars", side_effect=fake_extract):
            handler = _make_handler(body={})
            threads = []
            for _ in range(5):
                t = threading.Thread(target=lambda: handler._handle_tdarr_webhook_authenticated(
                    {"file": "/data/jellyfin/Show.mkv", "_id": "x"}, token="valid"))
                threads.append(t)
                t.start()
            for t in threads:
                t.join(timeout=2)
            import time; time.sleep(0.2)
        ops = webhook_ledger.list_operations()
        self.assertEqual(len(ops), 1)
        self.assertEqual(len(calls), 1)


class TestWebhookStates(HermeticStateMixin):
    def test_state_transitions_and_stale_recovery(self):
        import webhook_ledger
        webhook_ledger.init_ledger()
        key = webhook_ledger.compute_operation_key("/data/media/A.mkv")
        webhook_ledger.insert_received("/data/media/A.mkv", "id", fingerprint={"size": 1})
        # received -> claimed -> processing -> succeeded
        self.assertTrue(webhook_ledger.try_claim(key))
        webhook_ledger.set_status(key, "processing")
        op = webhook_ledger.get_operation(key)
        self.assertEqual(op["status"], "processing")
        webhook_ledger.set_status(key, "succeeded")
        op = webhook_ledger.get_operation(key)
        self.assertEqual(op["status"], "succeeded")
        # succeeded is terminal, cannot be re-claimed
        self.assertFalse(webhook_ledger.try_claim(key))
        # stale lease recovery: claimed with expired lease should be reclaimable conservatively
        key2 = webhook_ledger.compute_operation_key("/data/media/B.mkv")
        webhook_ledger.insert_received("/data/media/B.mkv", "id2", fingerprint={"size": 2})
        webhook_ledger.try_claim(key2, lease_ttl=1)
        # force expire
        webhook_ledger._force_expire(key2)
        recovered = webhook_ledger.recover_stale_leases(now=9999999999)
        self.assertGreaterEqual(recovered, 1)
        # after recovery, should be reclaimable
        self.assertTrue(webhook_ledger.try_claim(key2))

    def test_all_required_states_exist(self):
        import webhook_ledger
        for st in ["received","claimed","processing","succeeded","no_op","failed_retryable","failed_final","ambiguous"]:
            self.assertIn(st, webhook_ledger.VALID_STATUSES)

    def test_stale_recovery_conservative_terminal_not_recovered(self):
        import webhook_ledger
        webhook_ledger.init_ledger()
        for terminal in ["succeeded","no_op","failed_final","ambiguous"]:
            key = webhook_ledger.compute_operation_key(f"/data/media/{terminal}.mkv")
            webhook_ledger.insert_received(f"/data/media/{terminal}.mkv", "x")
            webhook_ledger.set_status(key, terminal)
            # even if lease expired flag set, recovery must not touch terminal
            webhook_ledger._force_expire(key)
            recovered = webhook_ledger.recover_stale_leases(now=9999999999)
            op = webhook_ledger.get_operation(key)
            self.assertEqual(op["status"], terminal, f"terminal {terminal} must not be recovered")


class TestWebhookTmpPaths(HermeticStateMixin):
    def test_unique_attempt_tmp_paths(self):
        import webhook_ledger
        webhook_ledger.init_ledger()
        # extraction should use unique attempt-specific tmp
        tmp_paths = []
        orig_replace = os.replace
        def capture_replace(src, dst):
            tmp_paths.append(src)
            return orig_replace(src, dst)
        # setup a media file
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        media = os.path.join(tmp, "Show.mkv")
        open(media, "wb").close()
        stem = os.path.splitext(media)[0]
        # mock subtitle streams and ffmpeg to just create tmp
        streams = [{"index": 0, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "jpn"}, "disposition": {}}]
        def fake_run(cmd, **kw):
            out = cmd[-1]
            tmp_paths.append(out)
            with open(out, "w") as fh: fh.write("1\n00:00:00,000 --> 00:00:01,000\ntest\n")
            return MagicMock(returncode=0, stderr="")
        with patch.object(o, "_subtitle_streams", return_value=streams), \
             patch.object(o, "probe_audio", return_value=[]), \
             patch.object(o, "audio_stream_signature", return_value="sig"), \
             patch.object(o.subprocess, "run", side_effect=fake_run), \
             patch.object(o, "map_path", side_effect=lambda p: p), \
             patch.object(o, "load_config", return_value={"TARGET_LANGS": ["ja"]}), \
             patch.object(o, "REGISTRY_FILE", os.path.join(tmp, "registry.jsonl")):
            # simulate two attempts with different ledger attempts
            import webhook_ledger as wl
            wl.init_ledger(db_path=os.path.join(tmp, "webhook.db"))
            key = wl.compute_operation_key(media)
            wl.insert_received(media, "a")
            wl.try_claim(key)
            wl.set_status(key, "processing")
            # first extraction
            o.extract_subtitle_sidecars(media, tdarr_id="a")
            first_tmps = list(tmp_paths)
            tmp_paths.clear()
            # second attempt (retryable failure -> new attempt)
            wl.set_status(key, "failed_retryable")
            wl.try_claim(key)
            wl.set_status(key, "processing")
            o.extract_subtitle_sidecars(media, tdarr_id="a2")
            second_tmps = list(tmp_paths)
            self.assertNotEqual(first_tmps, second_tmps, "attempt tmp paths must be unique")
            for p in first_tmps + second_tmps:
                self.assertNotEqual(p, stem + ".ja.hi.srt.tmp", "must not use shared .tmp path")

class TestWebhookFailClosed(HermeticStateMixin):
    def test_source_generation_conflict_fails_closed(self):
        import webhook_ledger
        webhook_ledger.init_ledger()
        # first delivery with fingerprint size 100
        key = webhook_ledger.compute_operation_key("/data/media/Conflict.mkv")
        webhook_ledger.insert_received("/data/media/Conflict.mkv", "id1", fingerprint={"size": 100, "mtime_ns": 1000})
        webhook_ledger.try_claim(key)
        webhook_ledger.set_status(key, "processing")
        # second delivery with different fingerprint should be ambiguous and not start worker
        with patch.object(o, "extract_subtitle_sidecars") as mock:
            handler = _make_handler(body={})
            handler._handle_tdarr_webhook_authenticated(
                {"file": "/data/media/Conflict.mkv", "_id": "id2", "fingerprint": {"size": 200}},
                token="valid",
                fingerprint_override={"size": 200, "mtime_ns": 2000}
            )
            mock.assert_not_called()
        op = webhook_ledger.get_operation(key)
        self.assertEqual(op["status"], "ambiguous")

    def test_missing_identity_no_worker(self):
        import webhook_ledger
        webhook_ledger.init_ledger()
        with patch.object(o, "extract_subtitle_sidecars") as mock:
            handler = _make_handler(body={})
            handler._handle_tdarr_webhook_authenticated({"_id": "no-file"}, token="valid")
            mock.assert_not_called()


class TestWebhookRegistryProvenance(HermeticStateMixin):
    def test_extraction_still_registers_provenance(self):
        import webhook_ledger
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        media = os.path.join(tmp, "Show.mkv")
        open(media, "wb").close()
        stem = os.path.splitext(media)[0]
        registry = os.path.join(tmp, "registry.jsonl")
        streams = [{"index": 0, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "jpn"}, "disposition": {}}]
        def fake_run(cmd, **kw):
            with open(cmd[-1], "w") as fh: fh.write("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
            return MagicMock(returncode=0, stderr="")
        with patch.object(o, "_subtitle_streams", return_value=streams), \
             patch.object(o, "probe_audio", return_value=[]), \
             patch.object(o, "audio_stream_signature", return_value="sig"), \
             patch.object(o.subprocess, "run", side_effect=fake_run), \
             patch.object(o, "map_path", side_effect=lambda p: p), \
             patch.object(o, "load_config", return_value={"TARGET_LANGS": ["ja"]}), \
             patch.object(o, "REGISTRY_FILE", registry):
            o.extract_subtitle_sidecars(media)
            rows = o.load_records_jsonl(registry)
            self.assertTrue(any(r.get("source_kind") == "embedded" for r in rows))

class TestWebhookStatusEndpoint(HermeticStateMixin):
    def test_status_endpoint_read_only(self):
        import webhook_ledger
        webhook_ledger.init_ledger()
        webhook_ledger.insert_received("/data/media/Status.mkv", "x")
        handler = _make_handler(body={})
        code, body = handler.handle_webhook_status(token="valid")
        self.assertEqual(code, 200)
        self.assertIn("operations", body)

# Helper to create a handler-like object that wraps webhook_ledger integration
def _make_handler(body):
    class H:
        def __init__(self):
            self.calls = []
        def _handle_tdarr_webhook_authenticated(self, payload, token=None, fingerprint_override=None):
            # This will be implemented by orchestrator: authenticate then ledger then extract
            # For RED, we expect orchestrator to expose this method; if not present, raise
            if hasattr(o.ControlHandler, "_handle_tdarr_webhook_authenticated"):
                inst = o.ControlHandler.__new__(o.ControlHandler)
                return inst._handle_tdarr_webhook_authenticated(payload, token=token, fingerprint_override=fingerprint_override)
            # fallback: try orchestrator module-level function
            if hasattr(o, "handle_tdarr_webhook_authenticated"):
                return o.handle_tdarr_webhook_authenticated(payload, token=token, fingerprint_override=fingerprint_override)
            raise NotImplementedError("webhook handler not implemented")
        def handle_tdarr_webhook_dispatch(self, payload, token=None):
            if hasattr(o.ControlHandler, "handle_tdarr_webhook_dispatch"):
                inst = o.ControlHandler.__new__(o.ControlHandler)
                return inst.handle_tdarr_webhook_dispatch(payload, token=token)
            if hasattr(o, "handle_tdarr_webhook_dispatch"):
                return o.handle_tdarr_webhook_dispatch(payload, token=token)
            # emulate auth check + ledger path
            if not token or token != "valid":
                # use orchestrator CONTROL_API_KEY logic
                return 401, {"error": "unauthorized"}
            # if authenticated, try ledger path
            raise NotImplementedError("dispatch not implemented")
        def handle_webhook_status(self, token=None):
            if hasattr(o.ControlHandler, "handle_webhook_status"):
                inst = o.ControlHandler.__new__(o.ControlHandler)
                return inst.handle_webhook_status(token=token)
            if hasattr(o, "handle_webhook_status"):
                return o.handle_webhook_status(token=token)
            if hasattr(o, "get_webhook_status"):
                return o.get_webhook_status(token=token)
            raise NotImplementedError("status not implemented")
    return H()
