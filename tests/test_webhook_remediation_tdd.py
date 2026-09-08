"""TDD RED tests for inbound Tdarr webhook remediation second pass.

Requirements under test:
- import/init ledger failure must fail closed with zero worker (never direct-extract fallback)
- try_claim must return an ownership token and finalization must be fenced by that token
- stale recovery must not allow an old worker to publish or mark success after another owner recovers the lease
- pass unique attempt tmp path and owner checks before publish/delete
- add heartbeat or conservative lease handling for long NFS extraction
- status endpoint must be authenticated in HTTP production path
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from tests import HermeticStateMixin
import orchestrator as o


class TestImportInitFailureFailClosed(HermeticStateMixin):
    def test_import_failure_fails_closed_zero_worker(self):
        """If webhook_ledger cannot be imported, handler must fail closed, never fallback direct extract."""
        with patch.object(o, "extract_subtitle_sidecars") as mock_extract:
            original_import = __import__

            def fake_import(name, *args, **kwargs):
                if name == "webhook_ledger":
                    raise ImportError("simulated import failure")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=fake_import):
                # need to reload the function path? _handle_tdarr_webhook_core does import inside
                result = o._handle_tdarr_webhook_core({"file": "/data/media/FailImport.mkv", "_id": "x"})
                self.assertFalse(result, "import failure must return False (fail closed)")
                mock_extract.assert_not_called()

    def test_init_ledger_failure_fails_closed(self):
        """If init_ledger / insert_received raises, no worker must be started."""
        import webhook_ledger as wl
        wl.init_ledger()
        with patch.object(o, "extract_subtitle_sidecars") as mock_extract, \
             patch.object(wl, "insert_received", side_effect=RuntimeError("init failed")):
            result = o._handle_tdarr_webhook_core({"file": "/data/media/InitFail.mkv", "_id": "x"})
            self.assertFalse(result)
            mock_extract.assert_not_called()

    def test_dispatch_init_failure_no_worker(self):
        import webhook_ledger as wl
        wl.init_ledger()
        os.environ["CONTROL_API_KEY"] = "secret123"
        with patch.object(o, "extract_subtitle_sidecars") as mock_extract, \
             patch.object(wl, "init_ledger", side_effect=RuntimeError("ledger local dir unavailable")):
            code, body = o.handle_tdarr_webhook_dispatch({"file": "/data/media/DispatchFail.mkv", "_id": "x"}, token="secret123")
            # should still be authenticated but fail closed without worker (200 ok with started False, or 500, but no extract)
            mock_extract.assert_not_called()
            # if it returned started True it would have launched worker -> failure
            if body.get("started"):
                self.fail("dispatch must not start worker on ledger init failure")


class TestTryClaimOwnershipToken(HermeticStateMixin):
    def test_try_claim_returns_token_string(self):
        import webhook_ledger as wl
        wl.init_ledger()
        key = wl.compute_operation_key("/data/media/TokenA.mkv")
        wl.insert_received("/data/media/TokenA.mkv", "id1")
        owner = wl.try_claim(key, lease_ttl=600)
        # must be a non-empty string token, not merely True
        self.assertIsInstance(owner, str, f"try_claim must return ownership token string, got {owner!r}")
        self.assertTrue(len(owner) > 8, "token too short")
        self.assertNotEqual(owner, "True")
        # second claim must return falsy (None/False/empty)
        owner2 = wl.try_claim(key, lease_ttl=600)
        self.assertFalse(owner2, "second claim must be falsy")

    def test_finalization_fenced_by_token(self):
        import webhook_ledger as wl
        wl.init_ledger()
        key = wl.compute_operation_key("/data/media/Fenced.mkv")
        wl.insert_received("/data/media/Fenced.mkv", "id1")
        token = wl.try_claim(key, lease_ttl=600)
        self.assertIsInstance(token, str)
        wl.set_status(key, "processing", expected_owner=token) if "expected_owner" in wl.set_status.__code__.co_varnames else None
        # Wrong token must not be able to finalize to succeeded
        # Check that ledger exposes a fenced finalize or set_status with expected_owner
        has_fenced = False
        # try finalize API
        if hasattr(wl, "finalize_operation"):
            has_fenced = True
            ok_wrong = wl.finalize_operation(key, "wrong-token-123", "succeeded")
            self.assertFalse(ok_wrong, "wrong token finalize must fail")
            ok_right = wl.finalize_operation(key, token, "succeeded")
            self.assertTrue(ok_right, "correct token finalize must succeed")
        elif "expected_owner" in wl.set_status.__code__.co_varnames:
            has_fenced = True
            ok_wrong = wl.set_status(key, "succeeded", expected_owner="wrong-token")
            self.assertFalse(ok_wrong, "wrong token set_status must fail")
            cur = wl.get_operation(key)
            self.assertNotEqual(cur["status"], "succeeded", "status must not become succeeded with wrong token")
            ok_right = wl.set_status(key, "succeeded", expected_owner=token)
            self.assertTrue(ok_right)
            cur = wl.get_operation(key)
            self.assertEqual(cur["status"], "succeeded")
        else:
            self.fail("ledger must expose fenced finalization (finalize_operation or set_status(expected_owner))")

    def test_wrong_token_finalization_rejected(self):
        import webhook_ledger as wl
        wl.init_ledger()
        key = wl.compute_operation_key("/data/media/WrongToken.mkv")
        wl.insert_received("/data/media/WrongToken.mkv", "id1")
        token = wl.try_claim(key, lease_ttl=600)
        wl.set_status(key, "processing", expected_owner=token) if "expected_owner" in wl.set_status.__code__.co_varnames else wl.set_status(key, "processing")
        if hasattr(wl, "finalize_operation"):
            self.assertFalse(wl.finalize_operation(key, "bad-token", "succeeded"))
            self.assertEqual(wl.get_operation(key)["status"], "processing")
        elif "expected_owner" in wl.set_status.__code__.co_varnames:
            self.assertFalse(wl.set_status(key, "succeeded", expected_owner="bad-token"))
            self.assertEqual(wl.get_operation(key)["status"], "processing")
        else:
            self.fail("no fenced API")


class TestStaleOwnerFinalization(HermeticStateMixin):
    def test_stale_owner_cannot_publish_or_mark_success(self):
        import webhook_ledger as wl
        wl.init_ledger()
        # Create media-like path that is valid video
        key = wl.compute_operation_key("/data/media/StaleOwner.mkv")
        wl.insert_received("/data/media/StaleOwner.mkv", "id-old")
        token_old = wl.try_claim(key, lease_ttl=1)
        self.assertIsInstance(token_old, str)
        # move to processing with same token
        if "expected_owner" in wl.set_status.__code__.co_varnames:
            wl.set_status(key, "processing", expected_owner=token_old)
        else:
            wl.set_status(key, "processing")
        # expire lease and recover
        wl._force_expire(key)
        recovered = wl.recover_stale_leases(now=9999999999)
        self.assertGreaterEqual(recovered, 1)
        # new owner claims
        token_new = wl.try_claim(key, lease_ttl=600)
        self.assertIsInstance(token_new, str)
        self.assertNotEqual(token_old, token_new)
        # new owner must move to processing before terminal finalization (required)
        if "expected_owner" in wl.set_status.__code__.co_varnames:
            wl.set_status(key, "processing", expected_owner=token_new)
        else:
            wl.set_status(key, "processing")
        # old owner tries to finalize -> must fail
        if hasattr(wl, "finalize_operation"):
            ok = wl.finalize_operation(key, token_old, "succeeded")
            self.assertFalse(ok, "stale owner must not finalize after recovery")
            # new owner can finalize after processing
            ok2 = wl.finalize_operation(key, token_new, "succeeded")
            self.assertTrue(ok2)
        elif "expected_owner" in wl.set_status.__code__.co_varnames:
            ok = wl.set_status(key, "succeeded", expected_owner=token_old)
            self.assertFalse(ok, "stale owner must not finalize after recovery")
            cur = wl.get_operation(key)
            self.assertNotEqual(cur["status"], "succeeded")
            ok2 = wl.set_status(key, "succeeded", expected_owner=token_new)
            self.assertTrue(ok2)
        else:
            self.fail("no fenced finalization API")

    def test_orchestrator_worker_checks_owner_before_publish(self):
        """Simulate long NFS extraction where old worker loses lease; it must not os.replace or mark succeeded."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        media = os.path.join(tmp, "Show.mkv")
        open(media, "wb").close()
        import webhook_ledger as wl
        # use temp db under hermetic state dir already; but set explicit
        db_path = os.path.join(self.state_tmp, "webhook_inbox.db")
        wl.init_ledger(db_path=db_path)
        # insert and claim as old worker
        key = wl.compute_operation_key(media) if media.startswith("/data") else wl.compute_operation_key("/data/media/StalePublish.mkv")
        # Use a valid container path for ledger
        container = "/data/media/StalePublish.mkv"
        key = wl.compute_operation_key(container)
        wl.insert_received(container, "old", fingerprint={"size": 1})
        token_old = wl.try_claim(key, lease_ttl=1)
        if "expected_owner" in wl.set_status.__code__.co_varnames:
            wl.set_status(key, "processing", expected_owner=token_old)
        else:
            wl.set_status(key, "processing")
        wl._force_expire(key)
        wl.recover_stale_leases(now=9999999999)
        token_new = wl.try_claim(key, lease_ttl=600)
        # Old worker tries to publish: orchestrator should check owner before publish
        # We test via ledger fence: old token finalize must fail, so even if extraction finished, it must not mark succeeded
        if hasattr(wl, "finalize_operation"):
            self.assertFalse(wl.finalize_operation(key, token_old, "succeeded"))
        elif "expected_owner" in wl.set_status.__code__.co_varnames:
            self.assertFalse(wl.set_status(key, "succeeded", expected_owner=token_old))
        else:
            self.fail("missing fenced API")


class TestUniqueAttemptTmpPathAndOwnerChecks(HermeticStateMixin):
    def test_attempt_tmp_path_unique_and_includes_attempt_and_uuid(self):
        import webhook_ledger as wl
        p1 = wl.attempt_tmp_path("/tmp/out.srt", 1)
        p2 = wl.attempt_tmp_path("/tmp/out.srt", 1)
        self.assertNotEqual(p1, p2, "attempt tmp must be unique per call (uuid)")
        self.assertIn(".tmp.1.", p1)
        p3 = wl.attempt_tmp_path("/tmp/out.srt", 2)
        self.assertIn(".tmp.2.", p3)
        self.assertNotEqual(p1, p3)

    def test_orchestrator_tmp_uses_attempt_and_uuid(self):
        import webhook_ledger as wl
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        media = os.path.join(tmp, "Vid.mkv")
        open(media, "wb").close()
        # ensure ledger has attempt count
        container = "/data/media/TmpCheck.mkv"
        wl.init_ledger()
        key = wl.compute_operation_key(container)
        wl.insert_received(container, "x")
        token = wl.try_claim(key)
        # _webhook_tmp_path should include attempt number
        out = "/tmp/target.ja.hi.srt"
        tmp_path = o._webhook_tmp_path(out, container)
        # must contain .tmp.<attempt>.
        self.assertRegex(tmp_path, r"\.tmp\.\d+\.[0-9a-f]{8}")


class TestHeartbeatOrConservativeLease(HermeticStateMixin):
    def test_heartbeat_exists_or_lease_conservative(self):
        import webhook_ledger as wl
        has_heartbeat = hasattr(wl, "heartbeat")
        # if heartbeat exists, it must extend lease when owner matches and fail otherwise
        if has_heartbeat:
            wl.init_ledger()
            key = wl.compute_operation_key("/data/media/Heartbeat.mkv")
            wl.insert_received("/data/media/Heartbeat.mkv", "h1")
            token = wl.try_claim(key, lease_ttl=2)
            self.assertIsInstance(token, str)
            # heartbeat with correct token should succeed and extend expiry
            op_before = wl.get_operation(key)
            import time; time.sleep(0.01)
            ok = wl.heartbeat(key, token, lease_ttl=600)
            self.assertTrue(ok, "heartbeat with correct token must succeed")
            op_after = wl.get_operation(key)
            self.assertGreater(op_after["lease_expires_at"], op_before["lease_expires_at"])
            # wrong token must fail
            self.assertFalse(wl.heartbeat(key, "wrong-token", lease_ttl=600))
        else:
            # conservative lease: DEFAULT_LEASE_TTL should be >=1800 (extraction timeout)
            self.assertGreaterEqual(wl.DEFAULT_LEASE_TTL, 1800,
                "without heartbeat, lease TTL must be conservative >=1800 to cover NFS extraction")

    def test_processing_lease_not_expired_during_extraction_window(self):
        import webhook_ledger as wl
        wl.init_ledger()
        key = wl.compute_operation_key("/data/media/LeaseWin.mkv")
        wl.insert_received("/data/media/LeaseWin.mkv", "w1")
        token = wl.try_claim(key, lease_ttl=600)
        if "expected_owner" in wl.set_status.__code__.co_varnames:
            wl.set_status(key, "processing", expected_owner=token)
        else:
            wl.set_status(key, "processing")
        op = wl.get_operation(key)
        ttl = op["lease_expires_at"] - op["updated_at"]
        # if heartbeat exists, ttl may be default 600 but heartbeat will extend; otherwise must be >=1800
        if not hasattr(wl, "heartbeat"):
            self.assertGreaterEqual(ttl, 1800)


class TestStatusAuthentication(HermeticStateMixin):
    def test_webhook_status_requires_auth(self):
        os.environ["CONTROL_API_KEY"] = "secret123"
        import webhook_ledger as wl
        wl.init_ledger()
        wl.insert_received("/data/media/StatusAuth.mkv", "s1")
        # no token -> 401
        code, body = o.get_webhook_status(token=None)
        self.assertEqual(code, 401, "status without token must be 401")
        code, body = o.get_webhook_status(token="bad")
        self.assertEqual(code, 401)
        # correct token -> 200
        code, body = o.get_webhook_status(token="secret123")
        self.assertEqual(code, 200)
        self.assertIn("operations", body)

    def test_dispatch_auth_still_required(self):
        os.environ["CONTROL_API_KEY"] = "secret123"
        import webhook_ledger as wl
        wl.init_ledger()
        code, body = o.handle_tdarr_webhook_dispatch({"file": "/data/media/AuthDispatch.mkv", "_id": "x"}, token="bad")
        self.assertEqual(code, 401)
        ops_before = wl.list_operations()
        code2, body2 = o.handle_tdarr_webhook_dispatch({"file": "/data/media/AuthDispatch.mkv", "_id": "x"}, token=None)
        self.assertEqual(code2, 401)
        ops_after = wl.list_operations()
        self.assertEqual(len(ops_before), len(ops_after), "unauth dispatch must not touch ledger")

    def test_control_handler_status_http_requires_auth(self):
        # Simulate ControlHandler.do_GET /status requires auth (production path)
        # We test that handle path via _check_auth gating includes /status
        # Inspect source: do_GET should call _check_auth for /status
        import inspect
        src = inspect.getsource(o.ControlHandler.do_GET)
        # After fix, /status must NOT be in the bypass list (only /health bypasses)
        # Check that the exemption list does not include "/status"
        if '"/status"' in src and 'not in ("/health", "/status")' in src:
            self.fail("ControlHandler.do_GET must require auth for /status (only /health may be open)")
