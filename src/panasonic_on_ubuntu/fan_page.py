"""GTK fan controls and a draggable temperature/output curve."""
import json
import math
from gi.repository import Gtk, Gdk, GLib
from .fan import DEFAULT, PERFORMANCE, TEMPS

LABELS = {'follow':'随电源预设','standard':'标准','quiet':'静音','performance':'性能',
          'custom':'自定义曲线','manual':'手动输出'}


class Curve(Gtk.DrawingArea):
    def __init__(self):
        super().__init__(content_height=192, hexpand=True, focusable=True)
        self.values = list(DEFAULT['curve'])
        self.selected = 0
        self.start_y = 0
        self.dirty = False
        self.set_tooltip_text('拖动节点调整输出。方向键选择和调整节点。')
        self.set_draw_func(self.draw)
        drag = Gtk.GestureDrag()
        drag.connect('drag-begin', self.begin)
        drag.connect('drag-update', self.move)
        self.add_controller(drag)
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.key)
        self.add_controller(keys)

    def position(self, index):
        x = 42 + (self.get_width()-72) * (TEMPS[index]-TEMPS[0]) / (TEMPS[-1]-TEMPS[0])
        return x, 20 + (self.get_height()-54) * (100-self.values[index])/100

    def adjust(self, value):
        i = self.selected
        if i < len(TEMPS)-1:
            low = self.values[i-1] if i else 20
            self.values[i] = max(low, min(self.values[i+1], round(value/2)*2))
            self.dirty = True
        self.queue_draw()

    def begin(self, gesture, x, y):
        self.grab_focus()
        self.selected = min(range(len(TEMPS)), key=lambda i: abs(self.position(i)[0]-x))
        self.start_y = self.position(self.selected)[1]
        self.queue_draw()

    def move(self, gesture, dx, dy):
        self.adjust(100 - (self.start_y+dy-20) * 100 / max(1,self.get_height()-54))

    def key(self, controller, key, code, modifiers):
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
        cr.set_source_rgba(.45,.51,.55,.7);cr.move_to(42,20);cr.line_to(42,height-34);cr.stroke()
        for value in (0, 50, 100):
            y=20+(height-54)*(100-value)/100
            cr.set_source_rgba(.45,.51,.55,.24)
            cr.move_to(42,y);cr.line_to(width-30,y);cr.stroke()
            cr.set_source_rgb(.48,.52,.55);cr.move_to(1,y+4);cr.show_text(f'{value}%')
        points=[self.position(i) for i in range(len(TEMPS))]
        cr.set_source_rgb(.12,.60,.52);cr.set_line_width(3)
        for i,(x,y) in enumerate(points):
            (cr.move_to if not i else cr.line_to)(x,y)
        cr.stroke()
        for i,(x,y) in enumerate(points):
            cr.set_source_rgb(.10,.57,.49);cr.arc(x,y,7 if i==self.selected else 5,0,2*math.pi);cr.fill()
            cr.set_source_rgb(.48,.52,.55);cr.move_to(x-13,height-8);cr.show_text(f'{TEMPS[i]}°')
            if i==self.selected:
                cr.move_to(x-12,y-10 if y>28 else y+22);cr.show_text(f'{self.values[i]}%')


def build(app, page):
    data = lambda: app.get('power','fan') or {}
    def submit(**patch):
        app.control('fan-control', json.dumps(patch))
    app.metrics(page,[('CPU 温度',lambda: app.live('temperature') is not None and f"{app.live('temperature')} °C" or '—'),
                      ('风扇反馈',lambda: f"{data()['feedback']}%" if data().get('feedback') is not None else '—'),
                      ('转速',lambda: f"{app.get('firmware','fan_rpm')} RPM" if app.get('firmware','fan_rpm') is not None else '未知')])
    modes=app.card(page,'风扇模式')
    app.stat(modes,'当前',lambda: LABELS.get(data().get('mode'),{0:'标准',1:'静音'}.get(app.get('firmware','fan_profile'),'—')))
    app.actions(modes,[('标准',lambda:app.control('fan','standard')),('静音',lambda:app.control('fan','quiet')),
                       ('恢复预设',lambda:app.control('fan','restore'))])
    advanced=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=10)
    modes.append(advanced)
    app.actions(advanced,[('性能',lambda:submit(mode='performance')),('自定义曲线',lambda:submit(mode='custom')),('手动输出',lambda:submit(mode='manual'))])
    manual=app.card(page,'手动输出')
    base = ['/usr/bin/pkexec','/usr/libexec/pou-adjust']
    app.slider(manual,'风扇输出',lambda:data().get('percent'),[*base,'fan-percent'],20,100,step=2)
    custom=app.card(page,'温度与风扇输出')
    curve=Curve();custom.append(curve)
    row=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8)
    custom.append(row)
    response=app.slider(row,'响应时间',lambda:data().get('response'),[*base,'fan-response'],1,20,unit=' 秒')
    def apply_curve():
        submit(mode='custom',curve=list(curve.values))
        curve.dirty=False
    save=Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    custom.append(save)
    app.actions(save,[('应用曲线',apply_curve)])
    app.text(modes,lambda:data().get('error',''))
    seen=None
    def update():
        nonlocal seen
        d=data()
        available=bool(d.get('available'))
        mode=d.get('mode')
        advanced.set_visible(available)
        manual.set_visible(available and mode=='manual')
        custom.set_visible(available and mode in ('performance','custom'))
        curve.set_sensitive(mode=='custom')
        row.set_visible(mode=='custom')
        save.set_visible(mode=='custom')
        key=(d.get('request_id'),d.get('mode'))
        if seen==key or not d.get('available'):return
        seen=key
        if mode=='performance':
            curve.values=list(PERFORMANCE);curve.dirty=False;curve.queue_draw()
        elif not curve.dirty:
            curve.values=list(d.get('curve',DEFAULT['curve']));curve.queue_draw()
    app.updaters.append(update)
