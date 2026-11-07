import unittest
from panasonic_on_ubuntu import care


class SleepChargeTests(unittest.TestCase):
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
