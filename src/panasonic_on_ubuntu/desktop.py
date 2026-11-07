"""GNOME 49+ brightness API; runs in the user's desktop session."""
import sys
from gi.repository import Gio, GLib

NAME = 'org.gnome.Mutter.DisplayConfig'
PATH = '/org/gnome/Mutter/DisplayConfig'
PANEL = 'io.github.panasonic_on_ubuntu.Panel'
PANEL_PATH = '/io/github/panasonic_on_ubuntu/Panel'


def screen_percent(percent=None):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    args = None if percent is None else GLib.Variant('(i)', (percent,))
    method = 'GetBrightness' if percent is None else 'SetBrightness'
    try:
        result = bus.call_sync(PANEL, PANEL_PATH, PANEL, method, args, None,
                               Gio.DBusCallFlags.NO_AUTO_START, 1000, None)
    except GLib.Error as error:
        if Gio.DBusError.get_remote_error(error) not in (
                'org.freedesktop.DBus.Error.ServiceUnknown','org.freedesktop.DBus.Error.NameHasNoOwner',
                'org.freedesktop.DBus.Error.UnknownMethod'):
            raise
        serial, monitor = backlight()
        low, high = monitor['min'], monitor['max']
        if high <= low:
            raise RuntimeError('内屏亮度范围无效')
        if percent is None:
            return round((monitor['value'] - low) * 100 / (high - low))
        return set_backlight(serial, monitor['connector'], low + round(percent * (high-low)/100))
    if percent is None:
        value, = result.unpack()
        if value < 0:
            raise RuntimeError('内置屏幕亮度不可用')
        return value


def call(interface, method, args):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return bus.call_sync(NAME, PATH, interface, method, args, None,
                         Gio.DBusCallFlags.NONE, 3000, None).unpack()


def backlight():
    serial, monitors = call('org.freedesktop.DBus.Properties', 'Get',
                            GLib.Variant('(ss)', (NAME, 'Backlight')))[0]
    # Control the laptop's built-in display only.
    for monitor in monitors:
        if monitor.get('active') and monitor.get('connector', '').startswith(('eDP', 'LVDS', 'DSI')) and 'value' in monitor:
            return serial, monitor
    raise RuntimeError('内置屏幕亮度控制不可用，请打开系统显示设置。')


def target_value(monitor, direction):
    low, high = monitor['min'], monitor['max']
    if high <= low:
        raise ValueError('Invalid backlight range')
    step = max(1, round((high - low) * 0.05))
    return max(low, min(high, monitor['value'] + direction * step))


def set_backlight(serial, connector, value):
    call(NAME, 'SetBacklight', GLib.Variant('(usi)', (serial, connector, value)))


def brightness(direction):
    screen_percent(max(0, min(100, screen_percent() + direction * 5)))


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('Expected up, down or percentage 0..100')
        if sys.argv[1] in ('up', 'down'):
            brightness(1 if sys.argv[1] == 'up' else -1)
        else:
            percent = int(sys.argv[1])
            if percent not in range(101):
                raise ValueError('Brightness must be 0..100')
            screen_percent(percent)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
