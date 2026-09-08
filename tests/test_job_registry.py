import unittest

from orchestrator import (
    _queue_clear,
    _queue_enqueue,
    _queue_finish,
    _queue_lock,
    _queue_set_running,
    _queue_snapshot,
)


class TestJobRegistry(unittest.TestCase):
    def setUp(self):
        _queue_clear()

    def test_enqueue_preserves_order(self):
        _queue_enqueue({"kind": "movie", "title": "A", "lang": "ja"})
        _queue_enqueue({"kind": "movie", "title": "B", "lang": "id"})
        q = _queue_snapshot()
        self.assertEqual([j["title"] for j in q], ["A", "B"])
        self.assertEqual([j["state"] for j in q], ["queued", "queued"])

    def test_set_running_updates_state_and_since(self):
        _queue_enqueue({"kind": "movie", "title": "A", "lang": "ja"})
        _queue_set_running(0)
        j = _queue_snapshot()[0]
        self.assertEqual(j["state"], "running")
        self.assertIn("since", j)

    def test_finish_removes_entry(self):
        _queue_enqueue({"kind": "movie", "title": "A", "lang": "ja"})
        _queue_finish(0, ok=True)
        self.assertEqual(_queue_snapshot(), [])

    def test_snapshot_returns_copy(self):
        _queue_enqueue({"kind": "movie", "title": "A", "lang": "ja"})
        s = _queue_snapshot()
        s[0]["title"] = "mutated"
        self.assertEqual(_queue_snapshot()[0]["title"], "A")

    def test_tracked_flips_current_on_run_only(self):
        import orchestrator

        def work():
            self.assertIsNotNone(orchestrator._current)  # running now
            self.assertEqual(orchestrator._current["lang"], "ja")
            return "done"

        desc = {"kind": "movie", "title": "T", "lang": "ja", "stage": "translate"}
        fut = orchestrator._tracked(desc, work)
        # before scheduling nothing ran; after result _current is None
        self.assertEqual(fut.result(), "done")
        self.assertIsNone(orchestrator._current)

    def test_no_direct_current_writes_in_run_pass(self):
        import inspect, orchestrator
        src = inspect.getsource(orchestrator.run_pass)
        self.assertNotIn("_current =", src.replace('current": _current', ""),
                         "run_pass must not assign _current directly")

    def test_queue_summary_counts(self):
        from orchestrator import _queue_summary
        _queue_clear()
        _queue_enqueue({"kind": "movie", "title": "A", "lang": "ja"})
        _queue_enqueue({"kind": "series", "title": "B", "lang": "id"})
        s = _queue_summary()
        self.assertEqual(s["queued_total"], 2)
        self.assertEqual(len(s["items"]), 2)

    def test_queue_summary_running_flag(self):
        from orchestrator import _queue_summary
        _queue_clear()
        _queue_enqueue({"kind": "movie", "title": "A", "lang": "ja"})
        _queue_set_running(0)
        s = _queue_summary()
        self.assertTrue(s["running"])
        self.assertEqual(s["queued_total"], 0)

    def test_tracked_propagates_exception_and_clears(self):
        import orchestrator
        def bad():
            raise RuntimeError("boom")
        desc = {"kind": "movie", "title": "T", "lang": "ja", "stage": "translate"}
        fut = orchestrator._tracked(desc, bad)
        with self.assertRaises(RuntimeError):
            fut.result()
        self.assertIsNone(orchestrator._current)
        self.assertEqual(orchestrator._queue_snapshot(), [])


if __name__ == "__main__":
    unittest.main()
