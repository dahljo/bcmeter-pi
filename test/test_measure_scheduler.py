import unittest
from datetime import datetime
from unittest.mock import patch

from bcmeter.measure import _periodic_due, _timestamp_pair


class PeriodicDeadlineTest(unittest.TestCase):
    def test_large_forward_jump_requests_only_one_execution(self):
        interval = 0.25
        deadline = 100.25
        after_thirty_days = 100.0 + 30 * 24 * 60 * 60

        due, next_deadline = _periodic_due(
            after_thirty_days, deadline, interval,
        )

        self.assertTrue(due)
        self.assertGreater(next_deadline, after_thirty_days)
        self.assertLessEqual(next_deadline - after_thirty_days, interval)

    def test_backward_jump_does_not_make_future_deadline_due(self):
        deadline = 100.25

        due, next_deadline = _periodic_due(-2_592_000.0, deadline, 0.25)

        self.assertFalse(due)
        self.assertEqual(next_deadline, deadline)

    def test_invalid_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            _periodic_due(1.0, 1.0, 0.0)

    def test_csv_timestamp_still_uses_local_wall_clock(self):
        local_wall_time = datetime(2026, 7, 18, 14, 23, 45)
        with patch("bcmeter.measure.datetime") as wall_clock:
            wall_clock.now.return_value = local_wall_time
            self.assertEqual(_timestamp_pair(), ("18-07-26", "14:23:45"))


if __name__ == "__main__":
    unittest.main()
