"""Reconnectable status-panel entry point and unprivileged audio actions."""
import os
from pathlib import Path
import subprocess
import time
from gi.repository import Gio, GLib

UUID = 'panasonic-on-ubuntu@local'
PANEL = 'io.github.panasonic_on_ubuntu.Panel'
HIDDEN = Path(GLib.get_user_config_dir()) / 'panasonic-on-ubuntu' / 'indicator-hidden'


def native_visibility(method):
    try:
        Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(PANEL, '/io/github/panasonic_on_ubuntu/Panel', PANEL, method, None, None, Gio.DBusCallFlags.NO_AUTO_START, 1000, None)
        return True
    except GLib.Error:
        return False


def show_native():
    return native_visibility('Show')


def hide():
    HIDDEN.parent.mkdir(parents=True,exist_ok=True)
    HIDDEN.touch(mode=0o600)
    native_visibility('Hide')
    subprocess.run(['systemctl', '--user', 'stop', 'pou-tray.service'], check=True, timeout=10)


def show(autostart=False):
    if autostart:
        if HIDDEN.exists():
            return
    else:
        HIDDEN.unlink(missing_ok=True)
    if show_native():
        subprocess.run(['systemctl', '--user', 'stop', 'pou-tray.service'], timeout=5, check=False)
        return
    if Path('/usr/share/gnome-shell/extensions/' + UUID).is_dir() and 'GNOME' in os.environ.get('XDG_CURRENT_DESKTOP', '').upper():
        settings = Gio.Settings.new('org.gnome.shell')
        enabled = settings.get_strv('enabled-extensions')
        if UUID not in enabled:
            settings.set_strv('enabled-extensions', [*enabled, UUID])
            Gio.Settings.sync()
        subprocess.run(['gnome-extensions', 'enable', UUID], capture_output=True, timeout=5, check=False)
        for _ in range(5):
            if show_native():
                subprocess.run(['systemctl', '--user', 'stop', 'pou-tray.service'], timeout=5, check=False)
                return
            time.sleep(.2)
    # A first installation may need a GNOME login before Shell discovers it.
    subprocess.run(['systemctl', '--user', 'start', 'pou-tray.service'], check=True, timeout=10)


def audio(action, value):
    if action in ('volume', 'mic-volume'):
        level = int(value)
        if level not in range(101):
            raise ValueError('Volume must be 0..100')
        target = '@DEFAULT_SINK@' if action == 'volume' else '@DEFAULT_SOURCE@'
        command = 'set-sink-volume' if action == 'volume' else 'set-source-volume'
        args = [command, target, str(level) + '%']
    elif action in ('mute', 'mic-mute'):
        if value not in ('0', '1', 'toggle'):
            raise ValueError('Invalid mute state')
        args = ['set-sink-mute' if action == 'mute' else 'set-source-mute', '@DEFAULT_SINK@' if action == 'mute' else '@DEFAULT_SOURCE@', value]
    elif action in ('output', 'input'):
        index = int(value)
        if index < 0:
            raise ValueError('Invalid audio device')
        args = ['set-default-sink' if action == 'output' else 'set-default-source', str(index)]
    else:
        raise ValueError('Unknown audio action')
    subprocess.run(['pactl', *args], check=True, timeout=5)
