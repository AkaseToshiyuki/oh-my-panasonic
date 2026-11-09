import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from panasonic_on_ubuntu import statusbar


class StatusbarTests(unittest.TestCase):
    def test_preferences_round_trip_and_five_item_limit(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,XDG_CONFIG_HOME=directory):
            self.assertEqual(statusbar.load(),['battery'])
            items=['cpu','battery','remaining','cpu_power','memory']
            statusbar.save(items)
            self.assertEqual(statusbar.load(),items)
            for invalid in (items+['temperature'],['bad'],['cpu','cpu'],'cpu'):
                with self.assertRaises(ValueError):statusbar.save(invalid)
                self.assertEqual(statusbar.load(),items)
            self.assertEqual(list(statusbar.path().parent.glob('.statusbar-*')),[])
            statusbar.save([])
            self.assertEqual(statusbar.load(),[])

    def test_corrupt_preferences_recover_and_ignore_unknown_items(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,XDG_CONFIG_HOME=directory):
            statusbar.path().parent.mkdir(parents=True)
            for value in ('broken','null','[]','{"items":null}'):
                statusbar.path().write_text(value)
                self.assertEqual(statusbar.load(),['battery'])
            statusbar.path().write_text(json.dumps({'items':['bad',{},'cpu','cpu','battery']}))
            self.assertEqual(statusbar.load(),['cpu','battery'])
            statusbar.path().write_text(json.dumps({'items':list(statusbar.KEYS)}))
            self.assertEqual(len(statusbar.load()),5)

    def test_compact_values_hide_missing_and_keep_zero(self):
        values={'battery':85,'cpu':20,'remaining':10800,'battery_power':10,'cpu_power':0,'discharging':True}
        self.assertEqual(statusbar.render(list(values),values),'85% · CPU 20.0% · 剩余 3h · 放电 10.0W · CPU 0.0W')
        self.assertEqual(statusbar.render(['battery_power'],values,True),'充电 10.0W')
        self.assertEqual(statusbar.render(['temperature','cpu_power'],{'temperature':float('nan'),'cpu_power':None}),'')
        self.assertEqual(statusbar.render([],values),'')

    def test_external_power_estimate_requires_platform_power(self):
        self.assertEqual(statusbar.render(['remaining'],{'energy':30,'platform_power':10}),'约 3h')
        self.assertEqual(statusbar.render(['remaining'],{'energy':30,'cpu_power':2}),'')

    def test_icon_choice_defaults_on_and_survives_item_changes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,XDG_CONFIG_HOME=directory):
            self.assertTrue(statusbar.icon_enabled())
            statusbar.save(['battery'],touchpad_icon=False)
            statusbar.save(['cpu'])
            self.assertFalse(statusbar.icon_enabled())
            self.assertEqual(statusbar.load(),['cpu'])
            statusbar.save(['cpu'],touchpad_icon=True)
            self.assertTrue(statusbar.icon_enabled())
