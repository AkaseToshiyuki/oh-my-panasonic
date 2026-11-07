"""Native personal Wi-Fi connection UI; no root helper and no secret logging."""
from gi.repository import Gtk,Adw,GLib
from .wifi import NM,security,ssid,title,profile,personal_profile,password_valid,Activation


class Networks:
    def __init__(self,app,parent):
        self.app=app;self.client=None;self.task=None;self.scanning=False;self.signature=None
        self.rows=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8)
        self.status=Gtk.Label(label='正在读取附近网络…',xalign=0,wrap=True)
        self.actions=Gtk.Box(spacing=8,homogeneous=True)
        self.scan_button=Gtk.Button(label='刷新网络',hexpand=True)
        self.cancel_button=Gtk.Button(label='取消连接',hexpand=True,visible=False)
        self.scan_button.connect('clicked',lambda *_:self.scan())
        self.cancel_button.connect('clicked',lambda *_:self.task.cancel() if self.task else None)
        self.actions.append(self.scan_button);self.actions.append(self.cancel_button)
        self.hidden_button=Gtk.Button(label='隐藏网络',hexpand=True)
        self.hidden_button.connect('clicked',lambda *_:self.hidden())
        self.actions.append(self.hidden_button)
        parent.append(self.status);parent.append(self.rows);parent.append(self.actions)
        app.updaters.append(self.refresh)
        app.window.connect('close-request',self.closing)
        NM.Client.new_async(None,self.ready)

    def ready(self,source,result,*_):
        try:self.client=NM.Client.new_finish(result)
        except GLib.Error:self.status.set_label('网络服务不可用');return
        self.refresh()

    def devices(self):
        return [d for d in self.client.get_devices() if isinstance(d,NM.DeviceWifi)] if self.client else []

    def refresh(self):
        if not self.client:return
        devices=self.devices()
        self.scan_button.set_sensitive(bool(devices) and self.client.wireless_get_enabled() and not self.scanning and not self.task)
        self.hidden_button.set_sensitive(bool(devices) and self.client.wireless_get_enabled() and not self.task)
        entries={}
        for device in devices:
            active=device.get_active_access_point()
            for ap in device.get_access_points():
                if not ssid(ap):continue
                kind=security(ap);key=(ssid(ap),kind)
                connected=bool(active and active.get_path()==ap.get_path())
                item=(device,ap,connected,kind)
                if key not in entries or (connected,ap.get_strength())>(entries[key][2],entries[key][1].get_strength()):entries[key]=item
        ordered=sorted(entries.values(),key=lambda x:(not x[2],-x[1].get_strength(),title(x[1])))
        signature=tuple((d.get_path(),a.get_path(),a.get_strength(),active,kind) for d,a,active,kind in ordered)+(bool(self.task),self.client.wireless_get_enabled())
        if signature==self.signature:return
        self.signature=signature
        while self.rows.get_first_child():self.rows.remove(self.rows.get_first_child())
        if not self.task:
            self.status.set_label('请选择网络' if ordered else 'Wi-Fi 已关闭' if not self.client.wireless_get_enabled() else '暂无附近网络，请刷新')
        for device,ap,active,kind in ordered[:30]:
            row=Gtk.Box(spacing=10)
            name=Gtk.Label(label=title(ap),xalign=0,wrap=True,hexpand=True)
            name.set_max_width_chars(34);row.append(name)
            state=Gtk.Label(label=f"{ap.get_strength()}% · "+{'open':'开放','owe':'增强开放','wpa-psk':'WPA2','sae':'WPA3','enterprise':'企业认证','unsupported':'旧式认证'}[kind])
            row.append(state)
            button=Gtk.Button(label='已连接' if active else '连接',sensitive=not active and not self.task and self.client.wireless_get_enabled())
            button.connect('clicked',lambda _,d=device,a=ap:self.choose(d,a));row.append(button);self.rows.append(row)

    def scan(self):
        devices=self.devices()
        if self.scanning or self.task or not devices:return
        self.scanning=True;self.scan_button.set_sensitive(False);self.status.set_label('正在扫描…')
        remaining=len(devices);errors=[]
        def finished(device,result,*_):
            nonlocal remaining
            try:device.request_scan_finish(result)
            except GLib.Error:errors.append(True)
            remaining-=1
            if not remaining:
                self.scanning=False;self.signature=None;self.refresh()
                if errors:self.status.set_label('暂时无法扫描，请稍后重试')
        for device in devices:device.request_scan_async(None,finished)

    def choose(self,device,ap):
        if self.task:return
        existing=next((c for c in device.get_available_connections() if ap.connection_valid(c)),None)
        if existing:self.connect(device,ap,existing,False,False);return
        kind=security(ap)
        if kind in ('enterprise','unsupported'):
            self.app.settings('wifi');return
        dialog=Adw.MessageDialog(transient_for=self.app.window,heading='连接 Wi-Fi',body=title(ap))
        content=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=12)
        password=None
        if kind in ('wpa-psk','sae'):
            password=Gtk.PasswordEntry(show_peek_icon=True,hexpand=True)
            password.props.placeholder_text='Wi-Fi 密码';password.props.activates_default=True;content.append(password)
        remember=Gtk.CheckButton(label='记住此网络',active=True);content.append(remember)
        dialog.set_extra_child(content)
        dialog.add_response('cancel','取消');dialog.add_response('connect','连接')
        dialog.set_default_response('connect');dialog.set_close_response('cancel')
        dialog.set_response_appearance('connect',Adw.ResponseAppearance.SUGGESTED)
        if password:
            dialog.set_response_enabled('connect',False)
            password.connect('changed',lambda p:dialog.set_response_enabled('connect',password_valid(kind,p.get_text())))
        def response(_,answer):
            value=password.get_text() if password else ''
            if password:password.set_text('')
            if answer!='connect':return
            try:connection=profile(ap,value,remember.get_active())
            except (ValueError,GLib.Error):self.status.set_label('网络或密码格式不符合要求');return
            self.connect(device,ap,connection,True,remember.get_active())
        dialog.connect('response',response);dialog.present()
        if password:password.grab_focus()

    def hidden(self):
        devices=self.devices()
        if self.task or not devices:return
        dialog=Adw.MessageDialog(transient_for=self.app.window,heading='连接隐藏网络')
        content=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=12)
        name=Gtk.Entry(placeholder_text='网络名称（SSID）',hexpand=True);content.append(name)
        kinds=('wpa-psk','sae','owe','open')
        security_choice=Gtk.DropDown.new_from_strings(['WPA2 个人','WPA3 个人','增强开放（OWE）','开放网络']);content.append(security_choice)
        adapter=Gtk.DropDown.new_from_strings([d.get_iface() for d in devices])
        adapter.set_visible(len(devices)>1);content.append(adapter)
        password=Gtk.PasswordEntry(show_peek_icon=True,hexpand=True)
        password.props.placeholder_text='Wi-Fi 密码';content.append(password)
        remember=Gtk.CheckButton(label='记住此网络',active=True);content.append(remember)
        dialog.set_extra_child(content);dialog.add_response('cancel','取消');dialog.add_response('connect','连接')
        dialog.set_default_response('connect');dialog.set_close_response('cancel')
        dialog.set_response_appearance('connect',Adw.ResponseAppearance.SUGGESTED)
        def changed(*_):
            kind=kinds[security_choice.get_selected()];secret=kind in ('wpa-psk','sae')
            password.set_visible(secret)
            raw=name.get_text().encode('utf-8')
            dialog.set_response_enabled('connect',1<=len(raw)<=32 and b'\x00' not in raw and password_valid(kind,password.get_text() if secret else ''))
        name.connect('changed',changed);password.connect('changed',changed);security_choice.connect('notify::selected',changed);changed()
        def response(_,answer):
            kind=kinds[security_choice.get_selected()]
            value=password.get_text() if kind in ('wpa-psk','sae') else '';password.set_text('')
            if answer!='connect':return
            try:connection=personal_profile(name.get_text().encode('utf-8'),kind,value,remember.get_active(),hidden=True)
            except (ValueError,GLib.Error):self.status.set_label('网络或密码格式不符合要求');return
            self.connect(devices[adapter.get_selected()],None,connection,True,remember.get_active())
        dialog.connect('response',response);dialog.present();name.grab_focus()

    def connect(self,device,ap,connection,new,remember):
        if self.task:return
        def done(ok,message):
            self.task=None;self.cancel_button.set_visible(False)
            self.signature=None;self.refresh();self.status.set_label(message)
            self.app.release();self.app.refresh_once()
        def changed(message):
            self.status.set_label(message)
            self.cancel_button.set_sensitive(self.task.phase!='commit')
        self.task=Activation(self.client,device,ap,connection,new,remember,changed,done)
        self.app.hold();self.cancel_button.set_visible(True);self.cancel_button.set_sensitive(True)
        self.signature=None;self.refresh();self.task.start()

    def closing(self,*_):
        if self.task:self.task.cancel()
        return False
