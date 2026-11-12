import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from panasonic_on_ubuntu import fan


class FanControlTests(unittest.TestCase):
    def test_invalid_curves_and_types(self):
        for values in ([40, 50, 75, 70, 100], [40, 50, 65, 80, 90], [20, 40], [False, 50, 60, 80, 100]):
            with self.assertRaises(ValueError):
                fan.validate({'curve': values})
        self.assertEqual(fan.interpolate(42.5, [40, 60, 70, 80, 100]), 50)

    def test_smoothing_hot_override_and_release(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root/'config'
            config.write_text(json.dumps({'mode':'custom','response':20}))
            for name,val in {'fan_feedback_percent':52,'fan_profile':1,'package_temperature_c':35,'fan_pwm':0}.items():
                (root/name).write_text(str(val))
            controller = fan.Controller()
            with patch.object(fan,'FIRMWARE',root), patch.object(fan,'CONFIG',config), patch.object(fan.time,'monotonic',side_effect=[1,2,3]):
                self.assertEqual(controller.tick(1)['requested'],40)
                (root/'package_temperature_c').write_text('65')
                self.assertLess(controller.tick(1)['requested'],50)
                (root/'package_temperature_c').write_text('84')
                self.assertEqual(controller.tick(1)['requested'],100)
                config.write_text(json.dumps({'mode':'follow'}))
                controller.tick(1)
                self.assertEqual((root/'fan_pwm').read_text(),'0')
                self.assertEqual((root/'fan_profile').read_text(),'1')

    def test_unavailable_driver_keeps_existing_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(fan,'FIRMWARE',root), patch.object(fan,'CONFIG',root/'missing'):
                self.assertFalse(fan.Controller().tick(0)['available'])
                self.assertEqual(list(root.iterdir()),[])

    def test_sr3_quiet_renews_temperature_curve_and_releases_to_standard(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=root/'config'
            config.write_text(json.dumps({'mode':'follow'}))
            for name,value in {'fan_feedback_percent':20,'fan_profile':1,'package_temperature_c':35,
                               'fan_pwm':0,'software_quiet_curve':1}.items():
                (root/name).write_text(str(value))
            controller=fan.Controller()
            with patch.object(fan,'FIRMWARE',root),patch.object(fan,'CONFIG',config):
                self.assertEqual(controller.tick(1)['requested'],20)
                (root/'package_temperature_c').write_text('84')
                self.assertEqual(controller.tick(1)['requested'],100)
                controller.tick(0)
                self.assertEqual((root/'fan_pwm').read_text(),'0')
                self.assertEqual((root/'fan_profile').read_text(),'0')

if __name__ == '__main__':
    unittest.main()
