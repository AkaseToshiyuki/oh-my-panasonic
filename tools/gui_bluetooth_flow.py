"""End-to-end BlueZ protocol fixture. Run under a PRIVATE dbus-run-session."""
import os
if not os.environ.get('OMP_PRIVATE_BUS_TEST'):raise SystemExit('Use a private test bus; see docs/build.zh-CN.md')
os.environ['DBUS_SYSTEM_BUS_ADDRESS']=os.environ['DBUS_SESSION_BUS_ADDRESS']
import gi
gi.require_version('Gtk','4.0');gi.require_version('Adw','1')
from gi.repository import Gio,GLib,Gtk,Adw
from panasonic_on_ubuntu.bluetooth_page import Devices

ADAPTER='/org/bluez/hci0';DEVICE=ADAPTER+'/dev_fixture'
XML='''<node>
<interface name="org.freedesktop.DBus.ObjectManager"><method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method></interface>
<interface name="org.bluez.AgentManager1"><method name="RegisterAgent"><arg type="o" direction="in"/><arg type="s" direction="in"/></method><method name="UnregisterAgent"><arg type="o" direction="in"/></method></interface>
<interface name="org.bluez.Adapter1"><method name="StartDiscovery"/><method name="StopDiscovery"/><method name="RemoveDevice"><arg type="o" direction="in"/></method></interface>
<interface name="org.bluez.Device1"><method name="Pair"/><method name="CancelPairing"/><method name="Connect"/><method name="Disconnect"/></interface>
</node>'''
bus=Gio.bus_get_sync(Gio.BusType.SYSTEM,None)
bus.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','RequestName',GLib.Variant('(su)',('org.bluez',0)),None,0,3000,None)
objects={ADAPTER:{'org.bluez.Adapter1':{'Powered':GLib.Variant('b',True),'Discovering':GLib.Variant('b',False)}},
         DEVICE:{'org.bluez.Device1':{'Alias':GLib.Variant('s','Test Headset'),'Adapter':GLib.Variant('o',ADAPTER),'Paired':GLib.Variant('b',False),'Connected':GLib.Variant('b',False)}}}
agent=None;pending=None;events=[];errors=[];finished=False
def server(connection,sender,path,interface,method,args,invocation):
    global agent,pending
    events.append(method)
    if method=='GetManagedObjects':invocation.return_value(GLib.Variant('(a{oa{sa{sv}}})',(objects,)));return
    if method=='RegisterAgent':
        assert args.unpack()[1]=='KeyboardDisplay';agent=(sender,args.unpack()[0])
    elif method=='UnregisterAgent':agent=None
    elif method in ('StartDiscovery','StopDiscovery'):objects[ADAPTER]['org.bluez.Adapter1']['Discovering']=GLib.Variant('b',method=='StartDiscovery')
    elif method=='Pair':
        pending=invocation
        def answer(bus,result,*_):
            global pending
            if pending is not invocation:return
            pending=None
            try:bus.call_finish(result)
            except GLib.Error:invocation.return_dbus_error('org.bluez.Error.AuthenticationRejected','Rejected');return
            objects[DEVICE]['org.bluez.Device1']['Paired']=GLib.Variant('b',True)
            invocation.return_value(GLib.Variant('()',()))
        bus.call(agent[0],agent[1],'org.bluez.Agent1','RequestConfirmation',GLib.Variant('(ou)',(DEVICE,123456)),None,0,10000,None,answer);return
    elif method=='CancelPairing':
        if pending:pending.return_dbus_error('org.bluez.Error.AuthenticationCanceled','Cancelled');pending=None
    elif method in ('Connect','Disconnect'):objects[DEVICE]['org.bluez.Device1']['Connected']=GLib.Variant('b',method=='Connect')
    elif method=='RemoveDevice':assert args.unpack()[0]==DEVICE;del objects[DEVICE]
    invocation.return_value(GLib.Variant('()',()))
for interface in Gio.DBusNodeInfo.new_for_xml(XML).interfaces:
    path='/' if interface.name.endswith('ObjectManager') else '/org/bluez' if interface.name.endswith('AgentManager1') else ADAPTER if interface.name.endswith('Adapter1') else DEVICE
    bus.register_object(path,interface,server,None,None)

class App(Adw.Application):
    def do_activate(self):
        self.window=Adw.ApplicationWindow(application=self,title='Oh My Panasonic test');self.updaters=[]
        box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=12);self.window.set_content(box)
        self.controller=Devices(self,box);self.window.present()
app=App(application_id='io.github.ohmy_panasonic.BluetoothTest',flags=Gio.ApplicationFlags.NON_UNIQUE)
step=0;ticks=0
def advance():
    global step,ticks,finished
    ticks+=1
    try:
        c=app.controller
        if ticks>200:raise AssertionError(f'timed out at step {step}')
        if step==0 and DEVICE in c.objects:c.scan();step=1
        elif step==1 and c.scanning:c.stop_scan();step=2
        elif step==2 and not objects[ADAPTER]['org.bluez.Adapter1']['Discovering'].unpack():c.action(DEVICE,'Pair');step=3
        elif step==3 and c.dialog:c.dialog.response('cancel');step=4
        elif step==4 and not c.busy:
            assert not objects[DEVICE]['org.bluez.Device1']['Paired'].unpack();c.action(DEVICE,'Pair');step=5
        elif step==5 and c.dialog:c.dialog.response('allow');step=6
        elif step==6 and not c.busy:
            assert objects[DEVICE]['org.bluez.Device1']['Paired'].unpack();c.action(DEVICE,'Connect');step=7
        elif step==7 and not c.busy:
            assert objects[DEVICE]['org.bluez.Device1']['Connected'].unpack();c.action(DEVICE,'Disconnect');step=8
        elif step==8 and not c.busy:
            assert not objects[DEVICE]['org.bluez.Device1']['Connected'].unpack();c.remove(DEVICE,'Test Headset');step=9
        elif step==9:
            dialog=next(w for w in Gtk.Window.list_toplevels() if w is not app.window and hasattr(w,'get_body'))
            dialog.response('remove');step=10
        elif step==10 and DEVICE not in objects:
            assert 'RequestDefaultAgent' not in events
            print('BLUEZ_PRIVATE_BUS_PAIR_REJECT_CONFIRM_CONNECT_DISCONNECT_REMOVE_OK',flush=True)
            finished=True;app.window.close();return False
    except Exception as exc:errors.append(repr(exc));app.window.close();return False
    return True
GLib.timeout_add(100,advance);app.run(['omp-test'])
if errors or not finished:raise SystemExit(str(errors))
assert agent is None,'agent registration leaked'
print('BLUEZ_AGENT_CLEANUP_OK',flush=True)
