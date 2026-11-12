import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from panasonic_on_ubuntu import panel


class PanelStartupTests(unittest.TestCase):
    def test_history_initialization_is_once_even_with_hidden_indicator(self):
        with tempfile.TemporaryDirectory() as directory, \
            patch.object(panel,'HIDDEN',Path(directory)/'hidden'), \
            patch.object(panel.subprocess,'run') as run:
            panel.HIDDEN.touch()
            panel.show(autostart=True)
            panel.show(autostart=True)
            run.assert_called_once_with(['systemctl','--user','enable','--now','pou-monitor.timer'],check=True,timeout=10,capture_output=True)
            self.assertTrue((Path(directory)/'history-initialized').exists())

    def test_failed_initialization_retries_and_remove_allows_reinstall(self):
        with tempfile.TemporaryDirectory() as directory, \
            patch.object(panel,'HIDDEN',Path(directory)/'hidden'), \
            patch.object(panel.subprocess,'run',side_effect=[OSError('no bus'),None,None]) as run:
            panel.initialize_history()
            self.assertFalse((Path(directory)/'history-initialized').exists())
            panel.initialize_history()
            panel.initialize_history()
            self.assertEqual(run.call_count,2)
            panel.reset_history_initialization()
            panel.initialize_history()
            self.assertEqual(run.call_count,3)

    def test_native_first_install_waits_for_login_without_old_tray(self):
        with tempfile.TemporaryDirectory() as directory, \
            patch.object(panel,'HIDDEN',Path(directory)/'hidden'), \
            patch.dict(os.environ,{'XDG_CURRENT_DESKTOP':'ubuntu:GNOME'}), \
            patch.object(panel,'show_native',return_value=False), \
            patch.object(Path,'is_dir',return_value=True), \
            patch.object(panel.Gio.Settings,'new') as settings, \
            patch.object(panel.Gio.Settings,'sync'), \
            patch.object(panel.time,'sleep'), \
            patch.object(panel,'initialize_history'), \
            patch.object(panel.subprocess,'run') as run:
            settings.return_value.get_strv.return_value=['other-extension']
            panel.show(autostart=True)
            settings.return_value.set_strv.assert_called_once_with('enabled-extensions',['other-extension',panel.UUID])
            commands=[call.args[0] for call in run.call_args_list]
            self.assertNotIn(['systemctl','--user','start','pou-tray.service'],commands)
            self.assertIn(['systemctl','--user','stop','pou-tray.service'],commands)

    def test_other_desktops_keep_compatibility_tray(self):
        with tempfile.TemporaryDirectory() as directory, \
            patch.object(panel,'HIDDEN',Path(directory)/'hidden'), \
            patch.dict(os.environ,{'XDG_CURRENT_DESKTOP':'XFCE'}), \
            patch.object(panel,'show_native',return_value=False), \
            patch.object(panel,'initialize_history'), \
            patch.object(panel.subprocess,'run') as run:
            panel.show(autostart=True)
            run.assert_called_once_with(['systemctl','--user','start','pou-tray.service'],check=True,timeout=10)
