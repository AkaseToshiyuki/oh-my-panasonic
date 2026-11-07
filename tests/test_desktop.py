import unittest
from panasonic_on_ubuntu.desktop import target_value


class BrightnessTests(unittest.TestCase):
    def test_nonzero_hardware_minimum_and_step_clamp(self):
        monitor = {'min': 10, 'max': 1020, 'value': 929}
        self.assertEqual(target_value(monitor, -1), 879)
        self.assertEqual(target_value({**monitor, 'value': 11}, -1), 10)
        self.assertEqual(target_value({**monitor, 'value': 1010}, 1), 1020)

    def test_invalid_range_is_not_written(self):
        with self.assertRaises(ValueError):
            target_value({'min': 10, 'max': 10, 'value': 10}, -1)
