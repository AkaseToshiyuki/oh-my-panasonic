import unittest
from unittest.mock import MagicMock, patch

from panasonic_on_ubuntu import power_profiles as profiles

try:
    from gi.repository import Gio, GLib
except ImportError:
    Gio = GLib = None


@unittest.skipIf(Gio is None, 'requires Ubuntu PyGObject')
class PowerProfilesTests(unittest.TestCase):
    def test_get_set_are_bounded_and_never_auto_start_daemon(self):
        connection = MagicMock()
        connection.call_sync.return_value = GLib.Variant('(v)', (GLib.Variant('s', 'balanced'),))
        with patch.object(Gio, 'bus_get_sync', return_value=connection):
            self.assertEqual(profiles.get_active(), 'balanced')
            profiles.set_active('performance')
        connection.set_exit_on_close.assert_called_with(False)
        get, set_ = [call.args for call in connection.call_sync.call_args_list]
        self.assertEqual(get[0:4], (profiles.ENDPOINTS[0][0], profiles.ENDPOINTS[0][1],
                                  'org.freedesktop.DBus.Properties', 'Get'))
        self.assertEqual(get[4].unpack(), (profiles.ENDPOINTS[0][0], 'ActiveProfile'))
        self.assertEqual(set_[4].unpack(), (profiles.ENDPOINTS[0][0], 'ActiveProfile', 'performance'))
        for args in (get, set_):
            self.assertEqual(args[6], Gio.DBusCallFlags.NO_AUTO_START)
            self.assertEqual(args[7], 1500)

    def test_service_missing_falls_back_to_legacy_api(self):
        missing = Gio.DBusError.new_for_dbus_error('org.freedesktop.DBus.Error.ServiceUnknown', 'unavailable')
        connection = MagicMock()
        connection.call_sync.side_effect = [missing, GLib.Variant('(v)', (GLib.Variant('s', 'power-saver'),))]
        with patch.object(Gio, 'bus_get_sync', return_value=connection):
            self.assertEqual(profiles.get_active(), 'power-saver')
        self.assertEqual(connection.call_sync.call_args.args[0:2], profiles.ENDPOINTS[1])

    def test_disconnect_is_recoverable_and_does_not_escape_to_telemetry(self):
        failure = Gio.DBusError.new_for_dbus_error('org.freedesktop.DBus.Error.Disconnected', 'bus closed')
        with patch.object(Gio, 'bus_get_sync', side_effect=failure):
            with self.assertRaises(profiles.ProfilesUnavailable):
                profiles.get_active()
            self.assertIsNone(profiles.current_or_none())
            from panasonic_on_ubuntu import telemetry
            state = telemetry.system()
            self.assertIsNone(state['profile'])
            self.assertGreater(state['cpu_total'], 0)
            self.assertGreater(state['memory_total_gb'], 0)

    def test_access_denied_is_not_retried_as_disconnection(self):
        denied = Gio.DBusError.new_for_dbus_error('org.freedesktop.DBus.Error.AccessDenied', 'denied')
        connection = MagicMock()
        connection.call_sync.side_effect = denied
        with patch.object(Gio, 'bus_get_sync', return_value=connection):
            with self.assertRaises(profiles.ProfileError) as caught:
                profiles.set_active('performance')
        self.assertNotIsInstance(caught.exception, profiles.ProfilesUnavailable)
        self.assertEqual(connection.call_sync.call_count, 1)


class ProfileValidationTests(unittest.TestCase):
    def test_invalid_profile_cannot_reach_bus(self):
        with patch.object(profiles, '_property') as call:
            with self.assertRaises(ValueError):
                profiles.set_active('invalid')
        call.assert_not_called()
