"""Verify rapid slider changes keep the last target; no hardware commands."""
from panasonic_on_ubuntu.dashboard import App, box
from gi.repository import GLib, Gtk

Gtk.init()
app=App()
calls=[]
def execute(argv, done=None): calls.append((argv,done))
app.execute=execute
app.slider(box(),'charge',lambda:80,['mock-battery'],50,100)
scale=app.scales[('mock-battery',)]
def wait():
    loop=GLib.MainLoop()
    GLib.timeout_add(300,lambda:loop.quit() or False)
    loop.run()
scale.set_value(75);wait()
assert [x[0][-1] for x in calls]==['75']
scale.set_value(80);wait()
scale.set_value(90);wait()
assert len(calls)==1, 'Concurrent slider operations'
calls[0][1]()
assert [x[0][-1] for x in calls]==['75','90'], 'Newest slider target was lost'
calls[1][1]()
print('GUI_SLIDER_COALESCING_OK')
