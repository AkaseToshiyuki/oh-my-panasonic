import json
import re
import tomllib
import unittest
from collections import namedtuple
from types import SimpleNamespace
from panasonic_on_ubuntu import touchpad


class TouchpadTests(unittest.TestCase):
    def device(self):
        info = namedtuple('Info', 'bustype vendor product version')
        return SimpleNamespace(name='SYNA0103:00 06CB:CE53 Touchpad',
            info=info(0x18,0x06cb,0xce53,0x100),
            absinfo=lambda axis:SimpleNamespace(min=0,max=771,resolution=12))

    def test_correct_sr3_geometry_and_config_syntax(self):
        value=tomllib.loads(touchpad.configuration(1,1,'CFSR3-1'))
        self.assertTrue(re.fullmatch(value['device_name_regex'],self.device().name))
        self.assertFalse(re.fullmatch(value['device_name_regex'],self.device().name.replace('Touchpad','Mouse')))
        self.assertEqual(value['scroll']['coordinate_y_scale'],1.0)
        self.assertAlmostEqual(value['scroll']['detect_area_radius']/385.5,.8)
        self.assertTrue(value['scroll']['reverse_vertical'])

    def test_different_hardware_or_geometry_is_rejected(self):
        d=self.device()
        self.assertIsNotNone(touchpad.validate(d,'CFSR3-1'))
        for machine in ('CFSV7-3L','CFSR4-1',''):
            with self.assertRaises(ValueError):touchpad.validate(d,machine)
        d.absinfo=lambda axis:SimpleNamespace(min=0,max=1024,resolution=12)
        with self.assertRaises(ValueError):touchpad.validate(d,'CFSR3-1')

    def test_sv7_geometry_and_preferences_are_preserved(self):
        value=tomllib.loads(touchpad.configuration(-2,0,'CFSV7-3L'))
        self.assertEqual(value['scroll']['detect_area_radius'],1830)
        self.assertEqual(value['scroll']['minimum_rotation_radius'],500)
        self.assertEqual(value['scroll']['coordinate_y_scale'],1.332361516)
        self.assertEqual(value['scroll']['sensitivity'],-2)
        self.assertFalse(value['scroll']['reverse_vertical'])
        with self.assertRaises(ValueError):touchpad.configuration(0,1,'unsupported')
