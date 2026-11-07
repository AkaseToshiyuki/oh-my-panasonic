import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from panasonic_on_ubuntu.care_session import Session, continuous


class CareSessionTests(unittest.TestCase):
    def test_short_restart_preserves_observed_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'session.json'
            state = {'ac_since': 10}
            first = Session(path)
            with patch('panasonic_on_ubuntu.care_session.clock', return_value=('boot', 100)):
                first.prepare(state, 1000)
                self.assertNotIn('ac_since', state)
                state['ac_since'] = 1000
                first.checkpoint(state, 1000, True)
            restored = Session(path)
            with patch('panasonic_on_ubuntu.care_session.clock', return_value=('boot', 110)):
                restored.prepare(state, 1010)
            self.assertEqual(state['ac_since'], 1000)

    def test_gaps_clock_changes_reboot_and_changed_session_reset(self):
        record = dict(wall=1000, boot_seconds=100, ac_since=500, boot_id='boot')
        for wall, boot, seconds, state in ((1040,'boot',140,{'ac_since':500}),
                (1005,'other',105,{'ac_since':500}), (1010,'boot',105,{'ac_since':500}),
                (999,'boot',105,{'ac_since':500}), (1005,'boot',105,{})):
            self.assertFalse(continuous(record, state, wall, boot, seconds))
        self.assertTrue(continuous(record, {'ac_since':500}, 1005, 'boot', 105))

    def test_disconnect_or_bad_sample_clears_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'session.json'
            state = {'ac_since': 1000}
            session = Session(path)
            with patch('panasonic_on_ubuntu.care_session.clock', return_value=('boot', 100)):
                session.prepare(state, 1000)
                state['ac_since'] = 1000
                session.checkpoint(state, 1000, False)
                restored = Session(path)
                restored.prepare(state, 1000)
                self.assertNotIn('ac_since', state)

    def test_corrupt_checkpoint_does_not_prove_continuity(self):
        for record in ({}, [], None, {'wall':float('nan'),'boot_seconds':1,'ac_since':1}):
            self.assertFalse(continuous(record, {'ac_since':1}, 10, 'boot', 10))
