"""Asynchronous mode/scale choices using the desktop's own preview confirmation."""
import threading
from gi.repository import Gtk,GLib
from . import display_config as backend


class Displays:
    def __init__(self,app,parent):
        self.app=app;self.busy=False;self.closed=False;self.timer=0
        self.rows=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=12);parent.append(self.rows)
        self.message=Gtk.Label(xalign=0,wrap=True);parent.append(self.message)
        refresh=Gtk.Button(label='刷新显示器');parent.append(refresh);refresh.connect('clicked',lambda *_:self.refresh())
        app.window.connect('close-request',self.closing);self.refresh()

    def work(self,function,finished):
        self.app.hold()
        def run():
            try:value=function();error=None
            except Exception as exc:value=None;error=str(exc)
            def done():
                try:
                    if not self.closed:finished(value,error)
                finally:self.app.release()
                return False
            GLib.idle_add(done)
        threading.Thread(target=run,daemon=True).start()

    def refresh(self,message=''):
        if self.busy or self.closed:return
        self.busy=True
        def done(state,error):
            self.busy=False;self.rows.set_sensitive(True)
            if error:self.message.set_label('显示配置不可用');return
            self.message.set_label(message)
            while self.rows.get_first_child():self.rows.remove(self.rows.get_first_child())
            for monitor in state[1]:
                connector=monitor[0][0];modes=monitor[1]
                group=next((g for g in state[2] if any(s[0]==connector for s in g[5])),None)
                if not group:continue
                line=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8)
                line.append(Gtk.Label(label=monitor[2].get('display-name',connector),xalign=0))
                mode=Gtk.DropDown.new_from_strings([f'{m[1]} × {m[2]} · {m[3]:.2f} Hz' for m in modes]);line.append(mode)
                scale=Gtk.DropDown();line.append(scale)
                def changed(_,unused=None,choices=modes,widget=mode,scales=scale,current=group[2]):
                    values=choices[widget.get_selected()][5]
                    scales.set_model(Gtk.StringList.new([f'{s*100:.0f}%' for s in values]))
                    scales.set_selected(min(range(len(values)),key=lambda i:abs(values[i]-current)))
                mode.set_selected(next((i for i,m in enumerate(modes) if m[6].get('is-current')),0))
                mode.connect('notify::selected',changed);changed(None)
                button=Gtk.Button(label='预览更改');line.append(button)
                button.connect('clicked',lambda _,c=connector,choices=modes,m=mode,s=scale:self.begin(c,choices[m.get_selected()][0],choices[m.get_selected()][5][s.get_selected()]))
                self.rows.append(line)
        self.work(backend.read,done)

    def begin(self,connector,mode,scale):
        if self.busy or self.closed:return
        self.busy=True;self.rows.set_sensitive(False)
        def start():
            state=backend.read();configuration=backend.target(state,connector,mode,scale)
            if backend.same_layout(configuration,backend.layout(state)):return None
            preview=backend.Preview(state,configuration);preview.start();return preview
        def done(preview,error):
            if error or preview is None:
                self.busy=False;self.rows.set_sensitive(True);self.message.set_label(error or '已使用此显示设置');return
            self.message.set_label('请在系统确认框中保留或恢复显示设置')
            def settled():
                self.timer=0
                def finish(result,error):
                    self.busy=False
                    self.refresh(error or {'restored':'已恢复原显示设置','active':'显示设置已更新','changed':'显示设置已更改'}.get(result,''))
                self.work(preview.result,finish);return False
            self.timer=GLib.timeout_add_seconds(22,settled)
        self.work(start,done)

    def closing(self,*_):
        self.closed=True
        if self.timer:GLib.source_remove(self.timer);self.timer=0
        # GNOME owns the confirmation and restoration even after this window exits.
        return False
