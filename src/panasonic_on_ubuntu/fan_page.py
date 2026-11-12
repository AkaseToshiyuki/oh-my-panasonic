"""GTK fan controls and a draggable temperature/output curve."""
import json
import math
from gi.repository import Gtk, Gdk, GLib
from .fan import DEFAULT, TEMPS, plot

LABELS = {'follow':'随电源预设','standard':'标准','quiet':'静音','performance':'性能',
          'custom':'自定义曲线','manual':'手动输出'}


class Curve(Gtk.DrawingArea):
    def __init__(self):
        super().__init__(content_height=244, hexpand=True, focusable=False)
        self.values = list(DEFAULT['curve'])
        self.selected = 0
        self.start_y = 0
        self.dirty = False
        self.editable = False
        self.series = []
        self.firmware = False
        self.set_draw_func(self.draw)
        drag = Gtk.GestureDrag()
        drag.connect('drag-begin', self.begin)
        drag.connect('drag-update', self.move)
        self.add_controller(drag)
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.key)
        self.add_controller(keys)

    def position(self, index):
        return self.point(TEMPS[index], self.values[index])

    def point(self, temperature, value):
        x = 48 + (self.get_width()-78) * (temperature-20) / 70
        return x, 24 + (self.get_height()-82) * (100-value)/100

    def set_editable(self, editable):
        self.editable = editable
        self.set_focusable(editable)
        self.set_tooltip_text('拖动节点调整输出。方向键选择和调整节点。' if editable else None)

    def adjust(self, value):
        if not self.editable:
            return
        i = self.selected
        if i < len(TEMPS)-1:
            low = self.values[i-1] if i else 20
            self.values[i] = max(low, min(self.values[i+1], round(value/2)*2))
            self.dirty = True
        self.queue_draw()

    def begin(self, gesture, x, y):
        if not self.editable:
            return
        self.grab_focus()
        self.selected = min(range(len(TEMPS)), key=lambda i: abs(self.position(i)[0]-x))
        self.start_y = self.position(self.selected)[1]
        self.queue_draw()

    def move(self, gesture, dx, dy):
        if self.editable:
            self.adjust(100 - (self.start_y+dy-24) * 100 / max(1,self.get_height()-82))

    def key(self, controller, key, code, modifiers):
        if not self.editable:
            return False
        if key in (Gdk.KEY_Left,Gdk.KEY_Right):
            self.selected = max(0,min(len(TEMPS)-1,self.selected + (1 if key == Gdk.KEY_Right else -1)))
            self.queue_draw()
        elif key in (Gdk.KEY_Up,Gdk.KEY_Down):
            self.adjust(self.values[self.selected]+(2 if key==Gdk.KEY_Up else -2))
        else:
            return False
        return True

    def draw(self, area, cr, width, height):
        cr.set_font_size(12)
        cr.set_source_rgba(.45,.51,.55,.7);cr.move_to(48,24);cr.line_to(48,height-58);cr.stroke()
        for value in (0, 25, 50, 75, 100):
            y=self.point(20,value)[1]
            cr.set_source_rgba(.45,.51,.55,.24)
            cr.move_to(48,y);cr.line_to(width-30,y);cr.stroke()
            cr.set_source_rgb(.48,.52,.55);cr.move_to(1,y+4);cr.show_text(f'{value}%')
        for temperature in (20,35,50,65,80,90):
            cr.set_source_rgb(.48,.52,.55)
            cr.move_to(self.point(temperature,0)[0]-12,height-38);cr.show_text(f'{temperature}°C')
        colors=((.08,.58,.46),(.85,.43,.10),(.43,.38,.82),(.13,.48,.81))
        series = self.series
        if self.editable:
            points = list(zip(TEMPS,self.values))
            points = [p for p in points if p[0]<82]+[(82,100),(85,100)]
            series = [{'label':'CPU','points':points}]
        for index,item in enumerate(series):
            color=colors[index%len(colors)]
            cr.set_source_rgb(*color);cr.set_line_width(2.5)
            for i,(temperature,value) in enumerate(item['points']):
                x,y=self.point(temperature,value)
                (cr.move_to if not i else cr.line_to)(x,y)
            cr.stroke()
            for temperature,value in item['points']:
                x,y=self.point(temperature,value)
                cr.arc(x,y,3,0,2*math.pi);cr.fill()
        if self.firmware:
            cr.set_source_rgb(.48,.52,.55);cr.move_to(64,height/2);cr.show_text('固件自动')
        if self.editable:
            for i in range(len(TEMPS)):
                x,y=self.position(i)
                cr.set_source_rgb(*colors[0]);cr.arc(x,y,7 if i==self.selected else 5,0,2*math.pi);cr.fill()
                if i==self.selected:
                    cr.set_source_rgb(.48,.52,.55);cr.move_to(x-12,y-10 if y>34 else y+22);cr.show_text(f'{self.values[i]}%')


def build(app, page):
    data = lambda: app.get('power','fan') or {}
    def submit(**patch):
        app.control('fan-control', json.dumps(patch))
    app.metrics(page,[('CPU 温度',lambda: app.live('temperature') is not None and f"{app.live('temperature')} °C" or '—'),
                      ('风扇反馈',lambda: f"{data()['feedback']}%" if data().get('feedback') is not None else '—'),
                      ('转速',lambda: f"{app.get('firmware','fan_rpm')} RPM" if app.get('firmware','fan_rpm') is not None else '未知')])
    modes=app.card(page,'风扇模式')
    def current_mode():
        native = {0:'标准',1:'静音'}.get(app.get('firmware','fan_profile'),'—')
        return LABELS.get(data().get('mode'),native) if data().get('available') else native
    app.stat(modes,'当前',current_mode)
    app.actions(modes,[('标准',lambda:app.control('fan','standard')),('静音',lambda:app.control('fan','quiet')),
                       ('恢复预设',lambda:app.control('fan','restore'))])
    advanced=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=10)
    modes.append(advanced)
    app.actions(advanced,[('性能',lambda:submit(mode='performance')),('自定义曲线',lambda:submit(mode='custom')),('手动输出',lambda:submit(mode='manual'))])
    base = ['/usr/bin/pkexec','/usr/libexec/pou-adjust']
    custom=app.card(page,'温度与风扇输出')
    curve=Curve();custom.append(curve)
    legend=Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,homogeneous=True,
                       column_spacing=16,row_spacing=8,max_children_per_line=4)
    custom.append(legend)
    row=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8)
    custom.append(row)
    response=app.slider(row,'响应时间',lambda:data().get('response'),[*base,'fan-response'],1,20,unit=' 秒')
    def apply_curve():
        submit(mode='custom',curve=list(curve.values))
        curve.dirty=False
    save=Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    custom.append(save)
    app.actions(save,[('应用曲线',apply_curve)])
    manual=app.card(page,'手动输出')
    app.slider(manual,'风扇输出',lambda:data().get('percent'),[*base,'fan-percent'],20,100,step=2)
    app.text(modes,lambda:data().get('error',''))
    seen=None
    def update():
        nonlocal seen
        d=data()
        available=bool(d.get('available'))
        mode=d.get('mode')
        modes.set_visible(available or app.get('firmware','fan_profile') is not None)
        advanced.set_visible(available)
        manual.set_visible(available and mode=='manual')
        custom.set_visible(available or app.get('firmware','fan_profile') is not None)
        curve.set_editable(available and mode=='custom')
        row.set_visible(mode=='custom')
        save.set_visible(mode=='custom')
        view=d.get('plot') or plot({**DEFAULT,**d}, app.get('firmware','fan_profile') or 0)
        key=(d.get('request_id'),d.get('mode'),json.dumps(view,sort_keys=True))
        if seen==key:return
        if seen is not None and seen[1]!=mode:
            curve.dirty=False
        seen=key
        if not curve.dirty:
            curve.values=list(d.get('curve',DEFAULT['curve']))
        curve.series=view['series'];curve.firmware=view['firmware'];curve.queue_draw()
        while child:=legend.get_first_child():legend.remove(child)
        colors=('#149476','#d96e1a','#6e61d1','#217ace')
        for i,item in enumerate(curve.series):
            label=Gtk.Label(xalign=0)
            label.set_markup(f'<span foreground="{colors[i%4]}">●</span> {GLib.markup_escape_text(item["label"])}')
            legend.append(label)
    app.updaters.append(update)
