"""App-owned BlueZ agent; no default-agent takeover or implicit device trust."""
import threading
from gi.repository import Gio,GLib,Gtk,Adw

AGENT='/io/github/panasonic_on_ubuntu/Pairing'
XML='''<node><interface name="org.bluez.Agent1">
<method name="Release"/><method name="Cancel"/>
<method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
<method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
<method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
<method name="DisplayPasskey"><arg type="o" direction="in"/><arg type="u" direction="in"/><arg type="q" direction="in"/></method>
<method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
<method name="RequestAuthorization"><arg type="o" direction="in"/></method>
<method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
</interface></node>'''


def allowed(sender,owner,device,selected):
    return bool(owner and sender==owner and selected and device==selected)


def reply_value(method,text):
    if method=='RequestPinCode':
        if not 1<=len(text)<=16 or '\x00' in text:raise ValueError('PIN 需为 1–16 个字符')
        return GLib.Variant('(s)',(text,))
    if method=='RequestPasskey':
        if not text.isascii() or not text.isdigit() or not 1<=len(text)<=6:raise ValueError('请输入 1–6 位数字')
        return GLib.Variant('(u)',(int(text),))
    return GLib.Variant('()',())


def error_text(error):
    code=Gio.DBusError.get_remote_error(error) if isinstance(error,GLib.Error) else ''
    return {'org.bluez.Error.AuthenticationCanceled':'配对已取消',
        'org.bluez.Error.AuthenticationRejected':'配对被拒绝',
        'org.bluez.Error.AuthenticationFailed':'认证失败，请重新配对',
        'org.bluez.Error.AuthenticationTimeout':'配对超时，请重试',
        'org.bluez.Error.NotReady':'请先开启蓝牙',
        'org.bluez.Error.InProgress':'设备正在处理其他操作，请稍后重试',
        'org.bluez.Error.ConnectionAttemptFailed':'连接失败，请确认设备已开启',
        'org.bluez.Error.NotSupported':'此设备不支持该操作'}.get(code,'蓝牙操作未完成，请重试或打开系统蓝牙设置')


class Devices:
    def __init__(self,app,parent):
        self.app=app;self.bus=None;self.owner=None;self.objects={};self.selected=None
        self.closed=False;self.busy=False;self.reading=False;self.scanning=None;self.starting=False
        self.dialog=None;self.pending=None;self.prompt_timer=0;self.scan_timer=0;self.registration=0;self.signature=None
        self.status=Gtk.Label(label='正在读取蓝牙设备…',xalign=0,wrap=True);parent.append(self.status)
        self.rows=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=12);parent.append(self.rows)
        self.scan_button=Gtk.Button(label='扫描附近设备',sensitive=False);parent.append(self.scan_button)
        self.scan_button.connect('clicked',lambda *_:self.scan())
        self.cancel_button=Gtk.Button(label='取消配对',visible=False);parent.append(self.cancel_button)
        self.cancel_button.connect('clicked',lambda *_:self.cancel_pair())
        app.updaters.append(self.refresh);app.window.connect('close-request',self.closing)
        app.hold()
        def open_bus():
            try:
                address=Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM,None)
                connection=Gio.DBusConnection.new_for_address_sync(address,Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT|Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,None,None)
                connection.set_exit_on_close(False)
                owner=connection.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','GetNameOwner',GLib.Variant('(s)',('org.bluez',)),None,Gio.DBusCallFlags.NONE,3000,None).unpack()[0]
                error=None
            except Exception as exc:connection=None;owner=None;error=exc
            def ready():
                self.app.release()
                if self.closed:
                    if connection:connection.close(None,None)
                    return False
                if error:self.status.set_label('蓝牙服务不可用');return False
                self.bus=connection;self.owner=owner
                self.registration=self.bus.register_object(AGENT,Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0],self.agent,None,None)
                self.call('/org/bluez','org.bluez.AgentManager1','RegisterAgent',GLib.Variant('(os)',(AGENT,'KeyboardDisplay')),self.registered)
                return False
            GLib.idle_add(ready)
        threading.Thread(target=open_bus,daemon=True).start()

    def call(self,path,interface,method,args=None,done=None,timeout=10000):
        if not self.bus or self.closed:return
        def result(bus,value,*_):
            try:data=bus.call_finish(value).unpack();error=None
            except GLib.Error as exc:data=None;error=exc
            if not self.closed and done:done(data,error)
        self.bus.call(self.owner,path,interface,method,args,None,Gio.DBusCallFlags.NO_AUTO_START,timeout,None,result)

    def registered(self,data,error):
        if error:self.status.set_label(error_text(error));return
        self.scan_button.set_sensitive(True);self.refresh()

    def refresh(self):
        if not self.bus or self.closed or self.reading:return
        self.reading=True
        def done(data,error):
            self.reading=False
            if error:self.status.set_label(error_text(error));return
            self.objects=data[0];self.render()
        self.call('/','org.freedesktop.DBus.ObjectManager','GetManagedObjects',done=done)

    def render(self):
        entries=[]
        for path,interfaces in self.objects.items():
            device=interfaces.get('org.bluez.Device1')
            if device:
                battery=interfaces.get('org.bluez.Battery1',{}).get('Percentage')
                entries.append((path,device,battery))
        entries.sort(key=lambda d:(not d[1].get('Connected'),not d[1].get('Paired'),d[1].get('Alias','')))
        signature=tuple((p,d.get('Alias'),d.get('Connected'),d.get('Paired'),d.get('RSSI'),b) for p,d,b in entries)+(self.busy,)
        if signature==self.signature:return
        self.signature=signature
        while self.rows.get_first_child():self.rows.remove(self.rows.get_first_child())
        if self.status.get_label()=='正在读取蓝牙设备…':self.status.set_label('选择设备进行连接或配对' if entries else '暂无设备，请开启外设并扫描')
        for path,device,battery in entries[:40]:
            row=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=6)
            name=device.get('Alias') or device.get('Name') or '蓝牙设备'
            row.append(Gtk.Label(label=name,xalign=0,wrap=True))
            state='已连接' if device.get('Connected') else '已配对' if device.get('Paired') else '未配对'
            if battery is not None:state+=f' · 电量 {battery}%'
            if device.get('RSSI') is not None:state+=f" · {device['RSSI']} dBm"
            label=Gtk.Label(label=state,xalign=0);label.add_css_class('dim-label');row.append(label)
            actions=Gtk.Box(spacing=8,homogeneous=True)
            method='Disconnect' if device.get('Connected') else 'Connect' if device.get('Paired') else 'Pair'
            button=Gtk.Button(label={'Disconnect':'断开','Connect':'连接','Pair':'配对'}[method],sensitive=not self.busy)
            button.connect('clicked',lambda _,p=path,m=method:self.action(p,m));actions.append(button)
            if device.get('Paired'):
                button=Gtk.Button(label='移除',sensitive=not self.busy)
                button.connect('clicked',lambda _,p=path,n=name:self.remove(p,n));actions.append(button)
            row.append(actions);self.rows.append(row)

    def scan(self):
        if self.starting or self.busy:return
        if self.scanning:self.stop_scan();return
        adapter=next((p for p,i in self.objects.items() if i.get('org.bluez.Adapter1',{}).get('Powered')),None)
        if not adapter:self.status.set_label('请先开启蓝牙');return
        self.starting=True;self.scan_button.set_sensitive(False)
        def done(data,error):
            self.starting=False;self.scan_button.set_sensitive(True)
            if error:self.status.set_label(error_text(error));return
            self.scanning=adapter;self.scan_button.set_label('停止扫描');self.status.set_label('正在扫描附近设备…')
            self.scan_timer=GLib.timeout_add_seconds(30,self.stop_scan);self.refresh()
        self.call(adapter,'org.bluez.Adapter1','StartDiscovery',done=done)

    def stop_scan(self):
        if self.scan_timer:GLib.source_remove(self.scan_timer);self.scan_timer=0
        if self.scanning:
            adapter,self.scanning=self.scanning,None
            def done(data,error):
                self.status.set_label(error_text(error) if error else '扫描已结束');self.refresh()
            self.call(adapter,'org.bluez.Adapter1','StopDiscovery',done=done)
        self.scan_button.set_label('扫描附近设备');return False

    def action(self,path,method):
        if self.busy or self.closed:return
        self.busy=True;self.selected=path if method=='Pair' else None
        self.cancel_button.set_visible(method=='Pair');self.status.set_label('正在配对…' if method=='Pair' else '正在连接…' if method=='Connect' else '正在断开…')
        self.render();self.app.hold()
        # Hold the application until the bus callback or cleanup completes.
        def result(bus,value,*_):
            try:bus.call_finish(value);error=None
            except GLib.Error as exc:error=exc
            self.app.release()
            if self.closed:return
            self.reject_prompt();self.selected=None;self.busy=False;self.cancel_button.set_visible(False)
            self.refresh();self.status.set_label(error_text(error) if error else {'Pair':'配对成功，可连接设备','Connect':'已连接','Disconnect':'已断开'}[method])
        self.bus.call(self.owner,path,'org.bluez.Device1',method,None,None,Gio.DBusCallFlags.NO_AUTO_START,65000 if method=='Pair' else 25000,None,result)

    def cancel_pair(self):
        self.reject_prompt()
        if self.selected:self.call(self.selected,'org.bluez.Device1','CancelPairing')

    def remove(self,path,name):
        if self.busy:return
        dialog=Adw.MessageDialog(transient_for=self.app.window,heading='移除蓝牙设备？',body=name)
        dialog.add_response('cancel','取消');dialog.add_response('remove','移除');dialog.set_default_response('cancel');dialog.set_close_response('cancel')
        dialog.set_response_appearance('remove',Adw.ResponseAppearance.DESTRUCTIVE)
        def response(_,answer):
            if answer!='remove' or self.busy:return
            adapter=self.objects.get(path,{}).get('org.bluez.Device1',{}).get('Adapter')
            if not adapter:return
            self.busy=True;self.render()
            def done(data,error):
                self.busy=False;self.refresh();self.status.set_label(error_text(error) if error else '设备已移除')
            self.call(adapter,'org.bluez.Adapter1','RemoveDevice',GLib.Variant('(o)',(path,)),done)
        dialog.connect('response',response);dialog.present()

    def reject_prompt(self):
        if self.prompt_timer:GLib.source_remove(self.prompt_timer);self.prompt_timer=0
        if self.pending:
            invocation,self.pending=self.pending,None
            invocation.return_dbus_error('org.bluez.Error.Rejected','Pairing cancelled')
        if self.dialog:
            dialog,self.dialog=self.dialog,None;dialog.close()
        return False

    def agent(self,bus,sender,path,interface,method,parameters,invocation):
        values=parameters.unpack()
        if sender!=self.owner or self.closed:
            invocation.return_dbus_error('org.bluez.Error.Rejected','Not authorized');return
        if method in ('Cancel','Release'):
            self.reject_prompt();invocation.return_value(GLib.Variant('()',()));return
        if not values or not allowed(sender,self.owner,values[0],self.selected):
            invocation.return_dbus_error('org.bluez.Error.Rejected','No matching pairing request');return
        if method in ('DisplayPasskey','DisplayPinCode'):
            code=f'{values[1]:06d}' if method=='DisplayPasskey' else values[1]
            self.status.set_label(f'请在蓝牙设备输入 {code} 并确认'+(f' · 已输入 {values[2]} 位' if method=='DisplayPasskey' else ''))
            invocation.return_value(GLib.Variant('()',()));return
        self.reject_prompt();self.pending=invocation
        name=self.objects.get(values[0],{}).get('org.bluez.Device1',{}).get('Alias','蓝牙设备')
        body=f'{name}\n请核对配对码 {values[1]:06d}' if method=='RequestConfirmation' else name
        dialog=Adw.MessageDialog(transient_for=self.app.window,heading='确认蓝牙配对',body=body);self.dialog=dialog
        entry=None
        if method in ('RequestPinCode','RequestPasskey'):
            entry=Gtk.Entry(placeholder_text='输入设备上的 PIN' if method=='RequestPinCode' else '输入设备上的数字配对码',max_length=16 if method=='RequestPinCode' else 6)
            dialog.set_extra_child(entry)
        dialog.add_response('cancel','取消');dialog.add_response('allow','确认');dialog.set_default_response('cancel');dialog.set_close_response('cancel')
        if entry:
            def changed(widget):
                try:reply_value(method,widget.get_text());valid=True
                except ValueError:valid=False
                dialog.set_response_enabled('allow',valid)
            entry.connect('changed',changed);changed(entry)
        def response(_,answer):
            if self.pending is not invocation:return
            if answer=='allow':
                try:result=reply_value(method,entry.get_text() if entry else '')
                except ValueError:self.reject_prompt();return
                self.pending=None;invocation.return_value(result)
            self.reject_prompt()
        dialog.connect('response',response);self.prompt_timer=GLib.timeout_add_seconds(45,self.reject_prompt);dialog.present()

    def closing(self,*_):
        if self.closed:return False
        self.reject_prompt();self.closed=True
        if self.scan_timer:GLib.source_remove(self.scan_timer);self.scan_timer=0
        if not self.bus:return False
        bus=self.bus;self.app.hold()
        def cleanup():
            for path,iface,method,args in [(self.selected,'org.bluez.Device1','CancelPairing',None),
                (self.scanning,'org.bluez.Adapter1','StopDiscovery',None),
                ('/org/bluez','org.bluez.AgentManager1','UnregisterAgent',GLib.Variant('(o)',(AGENT,)))]:
                if path:
                    try:bus.call_sync(self.owner,path,iface,method,args,None,Gio.DBusCallFlags.NO_AUTO_START,1500,None)
                    except GLib.Error:pass
            try:bus.close_sync(None)
            except GLib.Error:pass
            GLib.idle_add(lambda:self.app.release())
        threading.Thread(target=cleanup,daemon=True).start();return False
