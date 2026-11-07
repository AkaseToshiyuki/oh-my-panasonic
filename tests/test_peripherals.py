import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from panasonic_on_ubuntu import peripherals as p


class PeripheralTests(unittest.TestCase):
    def test_cpu_policy_mapping_and_explicit_override(self):
        self.assertEqual(p.desired_policy('follow','eco'),'save')
        self.assertEqual(p.desired_policy('follow','performance'),'performance')
        self.assertEqual(p.desired_policy('follow','system'),'system')
        self.assertEqual(p.desired_policy('system','eco'),'system')
        with self.assertRaises(ValueError):p.desired_policy('unknown','system')

    def test_restore_respects_external_change(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline=Path(directory)/'baseline.json'
            baseline.write_text(json.dumps({'wifi:wlan0':{'original':'on','last':'off'}}))
            with patch.object(p,'BASELINE',baseline),patch.object(p,'devices',return_value={'wifi:wlan0':{'kind':'wifi','name':'wlan0','value':'on'}}),patch.object(p,'write') as writer:
                p.restore();writer.assert_not_called();self.assertFalse(baseline.exists())

    def test_failed_restore_retains_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline=Path(directory)/'baseline.json'
            baseline.write_text(json.dumps({'wifi:wlan0':{'original':'on','last':'off'}}))
            with patch.object(p,'BASELINE',baseline),patch.object(p,'devices',return_value={'wifi:wlan0':{'kind':'wifi','name':'wlan0','value':'off'}}),patch.object(p,'write',side_effect=OSError('disconnected')):
                with self.assertRaises(RuntimeError):p.restore()
                self.assertTrue(baseline.exists())
