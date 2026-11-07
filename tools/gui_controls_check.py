"""Cancel-only Wi-Fi inputs and app-owned Bluetooth confirmation UI checks."""
import os
os.environ['POU_NO_TRAY']='1';os.environ['GSETTINGS_BACKEND']='memory'
from unittest.mock import Mock
import gi
gi.require_version('Gtk','4.0');gi.require_version('Adw','1')
from gi.repository import Gio,GLib,Gtk
from panasonic_on_ubuntu.dashboard import App

app=App();app.set_flags(Gio.ApplicationFlags.NON_UNIQUE);errors=[];complete=False
def check():
    global complete
    try:
        networks=app.wifi_networks
        assert networks.devices(),'no Wi-Fi interface for input check'
        networks.hidden()
        dialog=next(w for w in Gtk.Window.list_toplevels() if w is not app.window and hasattr(w,'get_extra_child'))
        content=dialog.get_extra_child();name=content.get_first_child();security=name.get_next_sibling();adapter=security.get_next_sibling();password=adapter.get_next_sibling()
        name.set_text('Example Hidden Network');password.set_text('test-password')
        assert dialog.get_response_enabled('connect')
        name.set_text('过长'*20);assert not dialog.get_response_enabled('connect')
        dialog.response('cancel');assert password.get_text()=='' and networks.task is None
        bluetooth=app.bluetooth_devices
        assert bluetooth.bus and bluetooth.owner,'BlueZ agent not ready'
        bluetooth.selected='/test/selected'
        invocation=Mock()
        bluetooth.agent(None,bluetooth.owner,'','','RequestConfirmation',GLib.Variant('(ou)',('/test/selected',12345)),invocation)
        assert '012345' in bluetooth.dialog.get_body()
        bluetooth.dialog.response('allow')
        invocation.return_value.assert_called_once();invocation.return_dbus_error.assert_not_called()
        invocation=Mock()
        bluetooth.agent(None,bluetooth.owner,'','','RequestPasskey',GLib.Variant('(o)',('/test/selected',)),invocation)
        entry=bluetooth.dialog.get_extra_child();entry.set_text('123456')
        assert bluetooth.dialog.get_response_enabled('allow')
        bluetooth.dialog.response('cancel');invocation.return_dbus_error.assert_called_once()
        bluetooth.selected=None
        print('GUI_HIDDEN_WIFI_AND_BLUETOOTH_PROMPTS_OK',flush=True);complete=True
    except Exception as exc:errors.append(repr(exc))
    app.window.close();return False
GLib.timeout_add_seconds(4,check)
GLib.timeout_add_seconds(15,lambda:app.quit() or False)
app.run(['pou'])
if errors or not complete:raise SystemExit(str(errors))
