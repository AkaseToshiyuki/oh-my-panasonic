"""Render each actual GTK category at laptop-sized resolution."""
import os
from pathlib import Path
import subprocess
os.environ['POU_NO_TRAY']='1'
os.environ['GDK_BACKEND']='x11'
os.environ['GSK_RENDERER']='cairo'
from panasonic_on_ubuntu.dashboard import App
from gi.repository import GLib, GdkX11, Gio

app=App()
app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
destination=Path(os.environ.get('POU_SCREENSHOTS','/tmp/pou-pages'))
destination.mkdir(parents=True,exist_ok=True)
index=0
finished=False
def capture():
    global index,finished
    if index:
        window=str(app.window.get_surface().get_xid())
        subprocess.run(['import','-window',window,str(destination/(app.page_ids[index-1]+'.png'))],check=True,timeout=8)
    if index==len(app.page_ids):
        print('GUI_ALL_PAGES_RENDERED',flush=True)
        finished=True
        app.quit();return False
    app.nav.select_row(app.nav.get_row_at_index(index))
    index+=1
    return True
GLib.timeout_add_seconds(2,capture)
GLib.timeout_add_seconds(60,lambda:app.quit() or False)
app.run(None)
if not finished:raise SystemExit('Page capture did not complete')
