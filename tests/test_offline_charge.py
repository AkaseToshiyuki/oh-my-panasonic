import unittest
from pathlib import Path
from unittest.mock import patch
from panasonic_on_ubuntu import care


class SleepChargeTests(unittest.TestCase):
    def test_s2idle_requires_sr3_and_verified_driver_capability(self):
        from panasonic_on_ubuntu import hardware
        config = care.config_valid({'mode':'care','limit':85})
        for model,caps,expected in [('sr3','0',False),('sr3','3',True),('sv7','3',False)]:
            def read(path, *args):
                return caps if path.name=='offline_capabilities' else '[s2idle]'
            with patch.object(care,'read',side_effect=read),patch.object(hardware,'selection',return_value=(model,[])),patch.object(care,'load',side_effect=[config,{}]),patch.object(Path,'write_text') as write:
                care.arm_offline('suspend')
                self.assertEqual(write.called,expected)

    def test_only_active_care_with_a_real_limit_arms_hold(self):
        config = care.config_valid({'mode':'care', 'limit':80})
        self.assertTrue(care.offline_requested(config, {}, 123, 'sleep_hold'))
        for patch in [{'mode':'system'}, {'limit':100}, {'sleep_hold':False}]:
            self.assertFalse(care.offline_requested({**config, **patch}, {}, 123, 'sleep_hold'))

    def test_full_override_and_recovery_take_precedence(self):
        config = care.config_valid({'mode':'care'})
        for state in [{'override_until':124}, {'maintenance_phase':'full'}, {'verification_error':'EC readback'}]:
            self.assertFalse(care.offline_requested(config, state, 123, 'sleep_hold'))
        self.assertTrue(care.offline_requested(config, {'override_until':122}, 123, 'sleep_hold'))
