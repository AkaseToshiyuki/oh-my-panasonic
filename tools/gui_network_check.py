"""Isolated-memory settings and cancel-only Wi-Fi dialog verification."""
import os
os.environ['GSETTINGS_BACKEND']='memory'
os.environ['POU_NO_TRAY']='1'
import gi
gi.require_version('Gtk','4.0')
from gi.repository import Gio,GLib,Gtk
from panasonic_on_ubuntu.dashboard import App
from panasonic_on_ubuntu.wifi import SECURITY

app=App();app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
errors=[];complete=False
class AP:
    def get_ssid(self):return GLib.Bytes.new(b'Example Wi-Fi')
    def get_rsn_flags(self):return SECURITY.KEY_MGMT_PSK
    def get_wpa_flags(self):return 0
    def get_flags(self):return 1
class Device:
    def get_available_connections(self):return []

def check():
    global complete
    try:
        assert app.window.get_title()=='Oh My Panasonic'
        settings=app.color_settings
        scale,reading,times=app.night_controls
        scale.set_value(3100)
        assert settings.get_uint('night-light-temperature')==3100
        assert reading.get_label()=='3100 K'
        settings.set_uint('night-light-temperature',4200)
        assert scale.get_value()==4200
        settings.set_boolean('night-light-schedule-automatic',False)
        times[0][1].set_value(21);times[0][2].set_value(30)
        assert settings.get_double('night-light-schedule-from')==21.5
        app.wifi_networks.choose(Device(),AP())
        dialog=next(w for w in Gtk.Window.list_toplevels() if w is not app.window and hasattr(w,'get_extra_child'))
        child=dialog.get_extra_child().get_first_child()
        assert isinstance(child,Gtk.PasswordEntry)
        child.set_text('safe-test-password')
        assert dialog.get_response_enabled('connect')
        dialog.response('cancel')
        assert child.get_text()=='' and app.wifi_networks.task is None
        print('GUI_NETWORK_NIGHTLIGHT_OK',flush=True);complete=True
    except Exception as exc:errors.append(repr(exc))
    app.quit();return False

GLib.timeout_add_seconds(3,check)
app.run(['pou'])
if errors or not complete:raise SystemExit(str(errors))
