#!/usr/bin/env python3
"""Exercise PPD on an isolated bus; never connect to the real system bus.

Run: dbus-run-session -- env PYTHONPATH=src python3 tools/check_power_profiles_bus.py
"""
import os
import threading
import time
from unittest.mock import patch

os.environ['DBUS_SYSTEM_BUS_ADDRESS'] = os.environ['DBUS_SESSION_BUS_ADDRESS']
from gi.repository import Gio, GLib
from panasonic_on_ubuntu import power_profiles as profiles

interface, path = profiles.ENDPOINTS[0]
connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
connection.set_exit_on_close(False)
loop = GLib.MainLoop()
thread = threading.Thread(target=loop.run, daemon=True)
thread.start()
active = 'balanced'
info = Gio.DBusNodeInfo.new_for_xml(f'''<node><interface name="{interface}">
<property name="ActiveProfile" type="s" access="readwrite"/>
<property name="Profiles" type="aa{{sv}}" access="read"/>
</interface></node>''')


def get_property(connection, sender, path, interface, name):
    if name == 'ActiveProfile':
        return GLib.Variant('s', active)
    return GLib.Variant('aa{sv}', [{'Profile': GLib.Variant('s', p)} for p in profiles.PROFILES])


def set_property(connection, sender, path, interface, name, value):
    global active
    active = value.unpack()
    return True


registration = connection.register_object(path, info.interfaces[0], None, get_property, set_property)


def name_call(method):
    params = GLib.Variant('(su)', (interface, 0)) if method == 'RequestName' else GLib.Variant('(s)', (interface,))
    return connection.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                                'org.freedesktop.DBus', method, params, None,
                                Gio.DBusCallFlags.NONE, 1500, None).unpack()[0]


try:
    assert name_call('RequestName') == 1
    started = time.monotonic()
    assert profiles.available() == list(profiles.PROFILES)
    assert profiles.get_active() == 'balanced'
    profiles.set_active('power-saver')
    assert profiles.get_active() == 'power-saver'
    print(f'isolated get/set/readback: PASS ({(time.monotonic()-started)*1000:.1f} ms)')
    assert name_call('ReleaseName') == 1
    assert profiles.current_or_none() is None
    print('daemon disappeared: PASS (recoverable, no activation)')
    assert name_call('RequestName') == 1
    assert profiles.get_active() == 'power-saver'
    print('daemon returned: PASS')
    connection.unregister_object(registration)
    connection.close_sync(None)
    with patch.object(Gio, 'bus_get_sync', return_value=connection):
        assert profiles.current_or_none() is None
    print('closed bus connection: PASS (no process exit)')
finally:
    loop.quit()
    thread.join(timeout=2)
