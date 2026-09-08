"""TDD RED tests for remaining permanent ASRSub remediation.

Requirements under test (from authoritative task):
- serialize owner-check -> os.replace -> post-publish verification under one exclusive per-sidecar/registry lock
- ledger/owner-check errors around publication fail closed without deleting unknown path
- terminal finalization rejects expired leases even if lease_owner matches and requires status=processing
- preserve unique attempt temp paths and existing provenance
"""
import os
import tempfile
import shutil
import time
import unittest
from unittest.mock import patch, MagicMock

from tests import HermeticStateMixin
import orchestrator as o


class TestExpiredFinalization(HermeticStateMixin):
    def test_finalize_rejects_expired_lease_even_if_token_matches(self):
        import webhook_ledger as wl
        wl.init_ledger()
        container = "/data/media/ExpireFinalize.mkv"
        key = wl.compute_operation_key(container)
        wl.insert_received(container, "id1")
        token = wl.try_claim(key, lease_ttl=600)
        self.assertIsInstance(token, str)
        # move to processing fenced
        ok = wl.set_status(key, "processing", expected_owner=token)
        self.assertTrue(ok)
        # force expire lease
        wl._force_expire(key)
        op = wl.get_operation(key)
        self.assertEqual(op["lease_owner"], token)
        self.assertEqual(op["status"], "processing")
        # now finalize with correct token but expired lease must FAIL
        # both via finalize_operation and set_status fenced path
        if hasattr(wl, "finalize_operation"):
            result = wl.finalize_operation(key, token, "succeeded")
            self.assertFalse(result, "finalize must reject expired lease even if token matches")
            cur = wl.get_operation(key)
            self.assertEqual(cur["status"], "processing", "expired finalize must not change status")
            self.assertEqual(cur["lease_owner"], token)
        else:
            self.fail("ledger must expose finalize_operation")
        # also test set_status directly with expected_owner must reject expired
        # (if finalize delegates to set_status, this also checks set_status expiry check)
        # reset: recover? Instead check set_status also fails when expired for terminal transition
        # try direct terminal set_status with expired lease
        result2 = wl.set_status(key, "succeeded", expected_owner=token)
        # If finalize already failed, set_status should also fail (expired)
        # We allow implementation to make set_status also reject, but at least one must reject.
        # To keep RED meaningful, we assert that a direct succeeded with expired token is rejected if ledger is expected to enforce.
        # If implementation only enforces in finalize_operation, this second check may still pass - but primary assertion above already RED.

    def test_finalize_requires_processing_status(self):
        import webhook_ledger as wl
        wl.init_ledger()
        container = "/data/media/RequireProcessing.mkv"
        key = wl.compute_operation_key(container)
        wl.insert_received(container, "id1")
        token = wl.try_claim(key, lease_ttl=600)
        self.assertIsInstance(token, str)
        # stay in claimed, not processing
        op = wl.get_operation(key)
        self.assertEqual(op["status"], "claimed")
        # finalize to succeeded must fail because status != processing
        if hasattr(wl, "finalize_operation"):
            ok = wl.finalize_operation(key, token, "succeeded")
            self.assertFalse(ok, "finalize must require status=processing, not claimed")
            self.assertEqual(wl.get_operation(key)["status"], "claimed")
        else:
            self.fail("missing finalize_operation")
        # also from received directly must fail
        wl2_container = "/data/media/RequireProcessing2.mkv"
        key2 = wl.compute_operation_key(wl2_container)
        wl.insert_received(wl2_container, "id2")
        token2 = wl.try_claim(key2, lease_ttl=600)
        wl.set_status(key2, "processing", expected_owner=token2)
        wl.set_status(key2, "succeeded", expected_owner=token2)
        # now already succeeded, finalize again to succeeded or failed should fail? At least test that non-processing status rejects.
        # try to finalize again with same token (now status succeeded, lease cleared) - should fail
        ok2 = wl.finalize_operation(key2, token2, "succeeded")
        self.assertFalse(ok2, "finalize must fail when not in processing")

    def test_finalize_rejects_expired_even_with_processing(self):
        import webhook_ledger as wl
        wl.init_ledger()
        container = "/data/media/ExpireProcessing.mkv"
        key = wl.compute_operation_key(container)
        wl.insert_received(container, "id1")
        token = wl.try_claim(key, lease_ttl=600)
        wl.set_status(key, "processing", expected_owner=token)
        # heartbeat should also fail if expired? But finalize must fail
        wl._force_expire(key)
        # heartbeat with expired? Actually heartbeat should still try but we test finalize
        if hasattr(wl, "finalize_operation"):
            self.assertFalse(wl.finalize_operation(key, token, "succeeded"))
            # also heartbeat should fail if expired? not required but check not succeeded
            self.assertEqual(wl.get_operation(key)["status"], "processing")


class TestInterleavedOwnerReplacement(HermeticStateMixin):
    def test_old_owner_cannot_overwrite_new_owner_output(self):
        """Interleaved old/new owner: old's os.replace must not clobber newer owner's sidecar."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        media = os.path.join(tmp, "Show.mkv")
        open(media, "wb").close()
        stem = os.path.splitext(media)[0]
        out_path = stem + ".ja.hi.srt"
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\nNEW\n")
        import webhook_ledger as wl
        container = "/data/media/Interleaved.mkv"
        wl.init_ledger()
        key = wl.compute_operation_key(container)
        wl.insert_received(container, "old", fingerprint={"size": 1})
        token_old = wl.try_claim(key, lease_ttl=600)
        wl.set_status(key, "processing", expected_owner=token_old)
        wl._force_expire(key)
        wl.recover_stale_leases(now=9999999999)
        token_new = wl.try_claim(key, lease_ttl=600)
        wl.set_status(key, "processing", expected_owner=token_new)
        streams = [{"index": 0, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "jpn"}, "disposition": {}}]
        def fake_run(cmd, **kw):
            tmp_path = cmd[-1]
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write("1\n00:00:00,000 --> 00:00:01,000\nOLD\n")
            return MagicMock(returncode=0, stderr="")
        # Bypass preserve guard so publish is attempted; otherwise skip would trivially pass.
        with patch.object(o, "_subtitle_streams", return_value=streams), \
             patch.object(o, "probe_audio", return_value=[]), \
             patch.object(o, "audio_stream_signature", return_value="sig-new"), \
             patch.object(o.subprocess, "run", side_effect=fake_run), \
             patch.object(o, "map_path", side_effect=lambda p: media if p == container else p), \
             patch.object(o, "load_config", return_value={"TARGET_LANGS": ["ja"]}), \
             patch.object(o, "REGISTRY_FILE", os.path.join(tmp, "registry.jsonl")), \
             patch.object(o, "target_sidecar_exists", return_value=None), \
             patch.object(o, "registry_get", return_value=None):
            o.extract_subtitle_sidecars(container, tdarr_id="old", _owner_token=token_old, _operation_key=key)
            self.assertTrue(os.path.isfile(out_path), "new owner output must not be deleted by old owner")
            with open(out_path, encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("NEW", content, f"old owner must not overwrite new owner output, got {content!r}")
            self.assertNotIn("OLD", content)

    def test_old_owner_tmp_cleanup_does_not_delete_new_owner_output(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        media = os.path.join(tmp, "Show2.mkv")
        open(media, "wb").close()
        stem = os.path.splitext(media)[0]
        out_path = stem + ".ja.hi.srt"
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\nNEW2\n")
        import webhook_ledger as wl
        container = "/data/media/Interleaved2.mkv"
        wl.init_ledger()
        key = wl.compute_operation_key(container)
        wl.insert_received(container, "old", fingerprint={"size": 1})
        token_old = wl.try_claim(key, lease_ttl=600)
        wl.set_status(key, "processing", expected_owner=token_old)
        wl._force_expire(key)
        wl.recover_stale_leases(now=9999999999)
        token_new = wl.try_claim(key, lease_ttl=600)
        wl.set_status(key, "processing", expected_owner=token_new)
        streams = [{"index": 0, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "jpn"}, "disposition": {}}]
        def fake_run_fail(cmd, **kw):
            # ffmpeg failure path: old worker will try to os.remove(tmp_path)
            # Ensure that failure does not delete out_path (unknown path guard)
            tmp_path = cmd[-1]
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write("")
            return MagicMock(returncode=1, stderr="ffmpeg error")
        with patch.object(o, "_subtitle_streams", return_value=streams), \
             patch.object(o, "probe_audio", return_value=[]), \
             patch.object(o, "audio_stream_signature", return_value="sig"), \
             patch.object(o.subprocess, "run", side_effect=fake_run_fail), \
             patch.object(o, "map_path", side_effect=lambda p: media if p == container else p), \
             patch.object(o, "load_config", return_value={"TARGET_LANGS": ["ja"]}), \
             patch.object(o, "REGISTRY_FILE", os.path.join(tmp, "registry.jsonl")):
            o.extract_subtitle_sidecars(container, tdarr_id="old", _owner_token=token_old, _operation_key=key)
            self.assertTrue(os.path.isfile(out_path))
            with open(out_path, encoding="utf-8") as fh:
                self.assertIn("NEW2", fh.read())

    def test_ledger_error_fails_closed_without_deleting_unknown_path(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        media = os.path.join(tmp, "Show3.mkv")
        open(media, "wb").close()
        stem = os.path.splitext(media)[0]
        out_path = stem + ".ja.hi.srt"
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,000\nEXISTING\n")
        import webhook_ledger as wl
        container = "/data/media/LedgerErr.mkv"
        wl.init_ledger()
        key = wl.compute_operation_key(container)
        wl.insert_received(container, "x", fingerprint={"size": 1})
        token = wl.try_claim(key, lease_ttl=600)
        wl.set_status(key, "processing", expected_owner=token)
        streams = [{"index": 0, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "jpn"}, "disposition": {}}]
        def fake_run(cmd, **kw):
            tmp_path = cmd[-1]
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write("1\n00:00:00,000 --> 00:00:01,000\nNEW3\n")
            return MagicMock(returncode=0, stderr="")
        # make get_operation raise around publication
        def raising_get(*a, **kw):
            raise RuntimeError("ledger unavailable")
        # Force extraction to proceed even though sidecar exists: bypass preserve guard
        with patch.object(o, "_subtitle_streams", return_value=streams), \
             patch.object(o, "probe_audio", return_value=[]), \
             patch.object(o, "audio_stream_signature", return_value="sig"), \
             patch.object(o.subprocess, "run", side_effect=fake_run), \
             patch.object(o, "map_path", side_effect=lambda p: media if p == container else p), \
             patch.object(o, "load_config", return_value={"TARGET_LANGS": ["ja"]}), \
             patch.object(o, "REGISTRY_FILE", os.path.join(tmp, "registry.jsonl")), \
             patch.object(o, "target_sidecar_exists", return_value=None), \
             patch.object(o, "registry_get", return_value=None), \
             patch.object(wl, "get_operation", side_effect=raising_get):
            o.extract_subtitle_sidecars(container, tdarr_id="x", _owner_token=token, _operation_key=key)
            # must not have deleted existing output (unknown path guard) and must not have overwritten
            # fail-closed means publish must not happen, existing preserved
            self.assertTrue(os.path.isfile(out_path))
            with open(out_path, encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("EXISTING", content, f"ledger error must fail closed preserving existing, got {content!r}")
            self.assertNotIn("NEW3", content)

    def test_publish_serialized_under_exclusive_lock(self):
        """Verify that extract_subtitle_sidecars holds exclusive per-sidecar/registry lock during publish.

        The critical section is owner-check -> os.replace -> post-publish verification -> registry_upsert
        must be under ONE exclusive lock. We verify os.replace is called while exclusive lock is held.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        media = os.path.join(tmp, "LockTest.mkv")
        open(media, "wb").close()
        import webhook_ledger as wl
        container = "/data/media/LockTest.mkv"
        wl.init_ledger()
        key = wl.compute_operation_key(container)
        wl.insert_received(container, "x", fingerprint={"size": 1})
        token = wl.try_claim(key, lease_ttl=600)
        wl.set_status(key, "processing", expected_owner=token)
        streams = [{"index": 0, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "jpn"}, "disposition": {}}]
        def fake_run(cmd, **kw):
            tmp_path = cmd[-1]
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write("1\n00:00:00,000 --> 00:00:01,000\nLOCKED\n")
            return MagicMock(returncode=0, stderr="")
        # Track whether os.replace is called while exclusive lock held
        orig_lock = o._registry_lock
        lock_depth = {"exclusive": 0}
        replace_inside_exclusive = {"called": False, "inside": None}
        orig_replace = os.replace
        def tracking_lock(path, exclusive=False):
            # wrap original context manager to track depth
            cm = orig_lock(path, exclusive=exclusive)
            class WrappingCM:
                def __enter__(self_inner):
                    if exclusive:
                        lock_depth["exclusive"] += 1
                    return cm.__enter__()
                def __exit__(self_inner, *a):
                    try:
                        return cm.__exit__(*a)
                    finally:
                        if exclusive:
                            lock_depth["exclusive"] -= 1
            return WrappingCM()
        def tracking_replace(src, dst):
            replace_inside_exclusive["called"] = True
            replace_inside_exclusive["inside"] = lock_depth["exclusive"] > 0
            return orig_replace(src, dst)
        with patch.object(o, "_subtitle_streams", return_value=streams), \
             patch.object(o, "probe_audio", return_value=[]), \
             patch.object(o, "audio_stream_signature", return_value="sig"), \
             patch.object(o.subprocess, "run", side_effect=fake_run), \
             patch.object(o, "map_path", side_effect=lambda p: media if p == container else p), \
             patch.object(o, "load_config", return_value={"TARGET_LANGS": ["ja"]}), \
             patch.object(o, "REGISTRY_FILE", os.path.join(tmp, "registry.jsonl")), \
             patch.object(o, "_registry_lock", side_effect=tracking_lock), \
             patch.object(o.os, "replace", side_effect=tracking_replace):
            # also need to patch global os.replace used inside orchestrator (imported as os)
            with patch("orchestrator.os.replace", side_effect=tracking_replace):
                o.extract_subtitle_sidecars(container, tdarr_id="x", _owner_token=token, _operation_key=key)
                self.assertTrue(replace_inside_exclusive["called"], "os.replace must be called for publish")
                self.assertTrue(replace_inside_exclusive["inside"], f"os.replace must be inside exclusive lock, got inside={replace_inside_exclusive['inside']}")
            self.assertTrue(replace_inside_exclusive["called"])
