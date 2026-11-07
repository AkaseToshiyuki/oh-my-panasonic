import errno
from pathlib import Path
import tempfile
import unittest
from panasonic_on_ubuntu.doctor import battery_metrics, collect, read_value


def fields(**kwargs):
    return {key: {"value": str(value), "error": None} for key, value in kwargs.items()}


class DoctorTests(unittest.TestCase):
    def test_real_machine_units_and_health(self):
        result = battery_metrics(fields(energy_full=40310000, energy_full_design=42480000,
                                        energy_now=40220000, power_now=0, status="Not charging"))
        self.assertEqual(result["energy_full_wh"], 40.31)
        self.assertEqual(result["estimated_health_percent"], 94.89)
        self.assertNotIn("instantaneous_hours_remaining", result)

    def test_incompatible_units_are_not_combined(self):
        self.assertNotIn("estimated_health_percent", battery_metrics(fields(energy_full=40, charge_full_design=50)))
        self.assertNotIn("estimated_health_percent", battery_metrics(fields(energy_full=40, energy_full_design=0)))

    def test_charge_based_battery(self):
        result = battery_metrics(fields(charge_full=4000000, charge_full_design=5000000))
        self.assertEqual(result["estimated_health_percent"], 80)
        self.assertEqual(result["charge_full_ah"], 4)

    def test_remaining_time_requires_discharge(self):
        result = battery_metrics(fields(status="Discharging", energy_now=20000000, power_now=10000000))
        self.assertEqual(result["instantaneous_hours_remaining"], 2)

    def test_fixture_does_not_expose_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            battery = root / "sys/class/power_supply/BAT1"
            battery.mkdir(parents=True)
            (battery / "type").write_text("Battery")
            (battery / "serial_number").write_text("DO-NOT-COLLECT")
            report = collect(root, live=False)
            self.assertEqual(len(report["batteries"]), 1)
            self.assertNotIn("DO-NOT-COLLECT", str(report))
            self.assertIsNone(report["batteries"][0]["fields"]["temp"]["value"])
            self.assertEqual(read_value(root / "missing")["error"]["errno"], errno.ENOENT)


if __name__ == "__main__":
    unittest.main()
