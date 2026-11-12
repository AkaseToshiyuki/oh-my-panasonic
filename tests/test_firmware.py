import tempfile
import unittest
from pathlib import Path

from panasonic_on_ubuntu.firmware import fan_rpm


class FanSpeedTests(unittest.TestCase):
    def test_only_matching_tachometer_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sensor = root / 'hwmon0'
            identity = sensor / 'device/firmware_node/path'
            identity.parent.mkdir(parents=True)
            identity.write_text(r'\_SB_.PCI0.LPCB.EC0_.TFN1')
            (sensor / 'fan1_target').write_text('15000')
            self.assertIsNone(fan_rpm(root))

    def test_sr3_tachometer_identity_uses_pc00(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);sensor=root/'hwmon0'
            identity=sensor/'device/firmware_node/path';identity.parent.mkdir(parents=True)
            identity.write_text(r'\_SB_.PC00.LPCB.EC0_.TFN1')
            (sensor/'fan1_input').write_text('2700')
            self.assertEqual(fan_rpm(root),2700)
            measured = sensor / 'fan1_input'
            for value, expected in [('4294967295', None), ('-1', None), ('bad', None), ('0', 0), ('3140', 3140)]:
                measured.write_text(value)
                self.assertEqual(fan_rpm(root), expected)
            identity.write_text(r'\_SB_.PCI0.DOCK.FAN1')
            self.assertIsNone(fan_rpm(root))
