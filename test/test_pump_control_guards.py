import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bcmeter.pump import Pump


PUMP = (ROOT / "bcmeter/pump.py").read_text()


class PumpControlGuardTests(unittest.TestCase):
    def test_stall_recovery_applies_the_found_duty(self):
        self.assertIn("recovery_ok = found not in (None, 0)", PUMP)
        self.assertIn("self.set_duty(found)", PUMP)
        self.assertIn("need_sweep = not recovery_ok", PUMP)

    def test_p_hold_requires_four_seconds_in_one_direction(self):
        pump = Pump()
        self.assertEqual(pump._p_hold_step(0.080, 0.100, now=0.0), 0)
        self.assertEqual(pump._p_hold_step(0.080, 0.100, now=3.9), 0)
        self.assertEqual(pump._p_hold_step(0.080, 0.100, now=4.0), 1)
        self.assertEqual(pump._p_hold_step(0.080, 0.100, now=7.9), 0)
        self.assertEqual(pump._p_hold_step(0.100, 0.100, now=8.0), 0)
        self.assertEqual(pump._p_hold_step(0.120, 0.100, now=9.0), 0)
        self.assertEqual(pump._p_hold_step(0.120, 0.100, now=13.0), -1)


if __name__ == "__main__":
    unittest.main()
