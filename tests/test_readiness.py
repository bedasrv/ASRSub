"""Regression tests for readiness dependency gating."""

from unittest.mock import patch

from tests import HermeticStateMixin
import orchestrator as o


class TestReadiness(HermeticStateMixin):
    def test_media_check_failure_makes_readiness_not_ready(self):
        with patch.object(o, "_check_btrfs_state", return_value=(True, {})), \
                patch.object(o, "_check_nfs_mount", return_value=(False, {"error": "not mounted"})), \
                patch.object(o, "_check_ledger_integrity", return_value=(True, {})), \
                patch.object(o, "_paused", False):
            readiness = o.get_readiness()

        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["checks"]["nfs"]["ok"])

