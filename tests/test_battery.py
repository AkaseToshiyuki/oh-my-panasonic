import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from panasonic_on_ubuntu.battery import snapshot, save_sample, history, database


class BatteryTests(unittest.TestCase):
    def test_unplugged_and_removed_battery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, values in {'BAT1': {'type': 'Battery', 'present': '0'}, 'AC': {'type': 'Mains', 'online': '0'}}.items():
                (root / name).mkdir()
                for key, value in values.items():
                    (root / name / key).write_text(value)
            self.assertEqual(snapshot(root)['batteries'], [])
            self.assertFalse(snapshot(root)['ac_online'])
            (root / 'AC/online').write_text('1')
            self.assertTrue(snapshot(root)['ac_online'])

    def test_history_retention_and_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'XDG_STATE_HOME': tmp}):
            now = int(time.time())
            save_sample({'timestamp': now - 31 * 86400, 'batteries': []})
            sample = {'timestamp': now, 'batteries': []}
            save_sample(sample)
            save_sample(sample)
            self.assertEqual(history(), [sample])
            with database() as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM samples').fetchone()[0], 1)
