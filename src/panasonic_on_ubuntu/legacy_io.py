"""Upgrade-only removal of bandwidth limits owned by versions 0.17.5/0.17.6."""
import json
from pathlib import Path

BASELINE = Path('/run/panasonic-on-ubuntu/io-baseline.json')
PROPERTIES = ('IOReadBandwidthMax','IOWriteBandwidthMax')


class Bus:
    def call(self, path, interface, method, parameters):
        from gi.repository import Gio, GLib
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            bus.set_exit_on_close(False)
            return bus.call_sync('org.freedesktop.systemd1',path,interface,method,parameters,
                None,Gio.DBusCallFlags.NO_AUTO_START,1500,None).unpack()
        except GLib.Error as error: raise RuntimeError(error.message) from error

    def get(self):
        from gi.repository import GLib
        path, = self.call('/org/freedesktop/systemd1','org.freedesktop.systemd1.Manager',
                          'GetUnit',GLib.Variant('(s)',('user.slice',)))
        values, = self.call(path,'org.freedesktop.DBus.Properties','GetAll',
            GLib.Variant('(s)',('org.freedesktop.systemd1.Slice',)))
        return {p:dict(values[p]) for p in PROPERTIES}

    def set(self, mappings):
        from gi.repository import GLib
        items=[]
        for key,value in mappings.items():
            items.extend([(key,GLib.Variant('a(st)',[])),(key,GLib.Variant('a(st)',sorted(value.items())))])
        self.call('/org/freedesktop/systemd1','org.freedesktop.systemd1.Manager','SetUnitProperties',
                  GLib.Variant('(sba(sv))',('user.slice',True,items)))
        actual=self.get()
        if any(actual[k]!=v for k,v in mappings.items()):raise RuntimeError('旧磁盘限制尚未恢复')


def release(bus=None):
    if not BASELINE.exists():return
    saved=json.loads(BASELINE.read_text())
    bus=bus or Bus()
    current=bus.get()
    changes={}
    for key in PROPERTIES:
        if current[key].get(saved['device'])!=saved['last'][key]:continue
        value=dict(current[key])
        if saved['original'][key] is None:value.pop(saved['device'],None)
        else:value[saved['device']]=saved['original'][key]
        changes[key]=value
    if changes:bus.set(changes)
    BASELINE.unlink()


def retire():
    release()
    for path in ('/etc/panasonic-on-ubuntu/io-limit.json','/run/panasonic-on-ubuntu/io-limit.json'):
        Path(path).unlink(missing_ok=True)
