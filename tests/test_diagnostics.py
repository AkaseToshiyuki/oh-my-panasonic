import unittest
from panasonic_on_ubuntu.diagnostics import StateLog


class LogTests(unittest.TestCase):
    def test_stable_state_has_one_heartbeat_per_five_minutes(self):
        log = StateLog()
        self.assertEqual([t for t in range(0, 901, 2) if log.due(('system',''), t)], [0,300,600,900])

    def test_errors_recovery_and_controls_are_logged_immediately(self):
        log = StateLog()
        for t, key in enumerate([('eco',''), ('eco','sensor failed'), ('eco',''), ('system','')]):
            self.assertTrue(log.due(key, t))
        self.assertFalse(log.due(('system',''), 4))

    def test_clock_reversal_does_not_silence_logging(self):
        log = StateLog()
        self.assertTrue(log.due('healthy', 1000))
        self.assertTrue(log.due('healthy', 10))
