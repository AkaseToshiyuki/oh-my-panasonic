"""Headless GTK smoke test, exits nonzero on missing widgets/activation."""
import os
os.environ['POU_NO_TRAY'] = '1'
from panasonic_on_ubuntu.gui import App
from gi.repository import GLib, Gio
app = App()
app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
passed=False
def inspect():
    global passed
    assert app.get_active_window() is not None
    if not app.data.get('timestamp'):
        return True
    assert len(app.page_ids) == 11
    for page in app.page_ids:
        assert app.stack.get_child_by_name(page) is not None
    print('GUI_SMOKE_OK', flush=True)
    passed=True
    app.quit()
    return False
GLib.timeout_add_seconds(1, inspect)
GLib.timeout_add_seconds(45,lambda:app.quit() or False)
app.run(None)
if not passed:raise SystemExit('GUI smoke did not complete')
