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


if __name__ == "__main__":
    unittest.main()
