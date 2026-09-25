import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from history import History


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.history = History(":memory:")

    def tearDown(self) -> None:
        self.history.close()

    def test_failure_counter_heals_after_window(self) -> None:
        self.history.record_sweep("one", {"node": -1.0})
        self.history.record_sweep("two", {"node": -1.0})
        self.history.record_sweep("three", {"node": -1.0})
        self.assertEqual(3, self.history.consecutive_failures("node", 3, 1))
        self.assertEqual(0, self.history.consecutive_failures("node", 3, 0.000001))

    def test_success_resets_failure_counter(self) -> None:
        self.history.record_sweep("one", {"node": -1.0})
        self.history.record_sweep("two", {"node": -1.0})
        self.history.record_sweep("three", {"node": 10.0})
        self.assertEqual(0, self.history.consecutive_failures("node", 3, 1))


if __name__ == "__main__":
    unittest.main()
