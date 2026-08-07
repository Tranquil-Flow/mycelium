import unittest
from unittest.mock import patch

import probe


class LinuxPowerProbeTests(unittest.TestCase):
    @patch.object(probe, "IS_MAC", False)
    @patch.object(
        probe,
        "run",
        side_effect=["1\n1", "71\n40", "Discharging\nDischarging"],
    )
    def test_multiple_batteries_and_ac_sources(self, _mock_run):
        self.assertEqual(
            probe.get_power(),
            {
                "on_ac_power": True,
                "battery_pct": 56,
                "battery_time_remaining": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
