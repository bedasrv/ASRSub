import unittest
from unittest.mock import patch

import orchestrator as o


class TestTimelineRecalcOrder(unittest.TestCase):
    def test_run_pass_recalculates_before_upgrades(self):
        events = []
        cfg = {
            "TARGET_LANGS": ["id"], "TRANSLATE_API_KEY": "test-key",
            "MAX_EPS_PER_RUN": 1,
        }
        with patch.object(o, "load_config", return_value=cfg), patch.object(
            o, "consume_actions", return_value=set()
        ), patch.object(o, "load_state", return_value=[]), patch.object(
            o, "get_wanted", return_value={"total": 0, "data": []}
        ), patch.object(o, "parse_exclusions", return_value=set()), patch.object(
            o, "_order_pass_candidates", return_value=([], 0)
        ), patch.object(o, "reconcile_registry", return_value={
            "scanned": 0, "reconciled": 0, "invalid": 0
        }), patch.object(o, "run_embedded_srt_sweep", return_value={
            "extracted": 0, "scanned": 0, "failed": []
        }), patch.object(o, "run_jimaku_hunt", return_value=None), patch.object(
            o, "notify_webhook", return_value=None
        ), patch.object(o, "run_timeline_recalc", side_effect=lambda *_: events.append("recalc") or {
            "repaired": 0, "checked": 0, "failed": []
        }), patch.object(o, "run_upgrades", side_effect=lambda *_args, **_kwargs: events.append("upgrade") or {
            "upgraded": 0, "checked": 0
        }), patch.object(o, "log", return_value=None), patch.object(
            o, "_timeline_recalc_done", False
        ), patch.object(o, "_embedded_srt_sweep_done", False), patch.object(
            o, "_registry_reconcile_done", False
        ):
            o.run_pass()
        self.assertLess(events.index("recalc"), events.index("upgrade"), events)


if __name__ == "__main__":
    unittest.main()
