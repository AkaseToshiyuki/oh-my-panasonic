"""Bounded PPD property calls; never spawn a CLI or activate PPD during shutdown."""

ENDPOINTS = (
    ('org.freedesktop.UPower.PowerProfiles', '/org/freedesktop/UPower/PowerProfiles'),
    ('net.hadess.PowerProfiles', '/net/hadess/PowerProfiles'),
)
PROFILES = ('power-saver', 'balanced', 'performance')


class ProfileError(RuntimeError):
    pass


class ProfilesUnavailable(ProfileError):
    pass


def _property(name, value=None):
    from gi.repository import Gio, GLib

    try:
        connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        # A disappearing system bus is recoverable, not a reason to exit the app.
        connection.set_exit_on_close(False)
        for index, (interface, path) in enumerate(ENDPOINTS):
            parameters = (GLib.Variant('(ss)', (interface, name)) if value is None else
                          GLib.Variant('(ssv)', (interface, name, GLib.Variant('s', value))))
            try:
                reply = connection.call_sync(
                    interface, path, 'org.freedesktop.DBus.Properties',
                    'Get' if value is None else 'Set', parameters, None,
                    Gio.DBusCallFlags.NO_AUTO_START, 1500, None)
                return reply.unpack()[0] if value is None else None
            except GLib.Error as exc:
                remote = Gio.DBusError.get_remote_error(exc)
                if index == 0 and remote in (
                    'org.freedesktop.DBus.Error.ServiceUnknown',
                    'org.freedesktop.DBus.Error.NameHasNoOwner',
                    'org.freedesktop.DBus.Error.UnknownObject',
                    'org.freedesktop.DBus.Error.UnknownInterface',
                ):
                    continue
                raise
    except GLib.Error as exc:
        remote = Gio.DBusError.get_remote_error(exc) or ''
        if remote.endswith(('.AccessDenied', '.InvalidArgs', '.NotAuthorized')):
            raise ProfileError(f'电源模式操作失败：{exc.message}') from exc
        raise ProfilesUnavailable(f'系统电源服务暂时不可用：{exc.message}') from exc


def get_active():
    return _property('ActiveProfile')


def set_active(profile):
    if profile not in PROFILES:
        raise ValueError('Invalid power profile')
    _property('ActiveProfile', profile)


def available():
    return [item['Profile'] for item in _property('Profiles') if item.get('Profile') in PROFILES]


def current_or_none():
    try:
        return get_active()
    except ProfileError:
        return None
