"""Card based GTK4 control center, using the same telemetry as the panel."""
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import gi
from .readings import text as fmt
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio, GLib, Gdk, Pango, PangoCairo

MODES = {'system':'系统管理','eco':'极致省电','balanced':'日常均衡','performance':'性能','adaptive':'温控性能','boost':'增强性能'}
BATTERY_STATES = {'Charging':'正在充电','Discharging':'电池供电','Full':'电池已满','Not charging':'已接电 · 未充电'}


def box(vertical=True, spacing=12):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)


def label(text='', style=None):
    return Gtk.Label(label=text, xalign=0, wrap=True, css_classes=[style] if style else [])


class Chart(Gtk.DrawingArea):
    def __init__(self, maximum, unit):
        super().__init__(content_height=135, hexpand=True)
        self.points, self.maximum, self.unit = [], maximum, unit
        self.set_draw_func(self.draw)

    def draw(self, area, cr, width, height):
        peak = max((p[1] for p in self.points), default=0)
        maximum = self.maximum or 1
        if peak > 0 and (not self.maximum or peak > maximum):
            step = 10 ** math.floor(math.log10(peak))
            maximum = next(n * step for n in (1,2,4,5,8,10) if n * step >= peak)
        ticks = [f'{maximum*f:.3g}' if maximum < 1 else f'{maximum*f:g}' for f in (1,.5,0)]
        cr.set_font_size(12)
        left = max(cr.text_extents(t).width for t in ticks) + 14
        top, bottom = 10, height-24
        color = self.get_color()
        cr.set_source_rgba(color.red,color.green,color.blue,.65)
        cr.move_to(left,top);cr.line_to(left,bottom);cr.stroke()
        cr.set_font_size(11)
        for text, fraction in zip(ticks,(0,.5,1)):
            y=top+(bottom-top)*fraction
            cr.set_source_rgba(.5,.5,.5,.2);cr.move_to(left,y);cr.line_to(width,y);cr.stroke()
            cr.set_source_rgba(color.red,color.green,color.blue,.9);cr.move_to(left-8-cr.text_extents(text).width,y+4);cr.show_text(text)
        if len(self.points)<2:
            layout=PangoCairo.create_layout(cr)
            layout.set_text('等待更多实测样本…',-1)
            cr.move_to(left+10,height/2-10);PangoCairo.show_layout(cr,layout);return
        start,end=self.points[0][0],self.points[-1][0]
        cr.set_source_rgb(.12,.62,.52);cr.set_line_width(2.5)
        for i,(stamp,value) in enumerate(self.points):
            x=left+(width-left)*(stamp-start)/max(1,end-start)
            y=bottom-(bottom-top)*min(maximum,max(0,value))/maximum
            (cr.line_to if i else cr.move_to)(x,y)
        cr.stroke()
        from datetime import datetime
        cr.set_source_rgba(.55,.58,.62,1)
        cr.move_to(left,height-5);cr.show_text(datetime.fromtimestamp(start).strftime('%H:%M'))
        cr.move_to(width-80,height-5);cr.show_text(datetime.fromtimestamp(end).strftime('%H:%M'))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id='io.github.panasonic_on_ubuntu',flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.data, self.updaters, self.cards = {}, [], []
        self.page_updates, self.page_cards = {}, {}
        self.refresh_pending = False
        self.rx_points, self.tx_points, self.net_previous = [], [], None
        self.scales = {}
        self.updating = self.loading = False
        self.connect('activate',self.activate)
        self.connect('command-line',self.command_line)

    def command_line(self,app,command):
        self.activate()
        arguments=command.get_arguments()[1:]
        if arguments and arguments[0] in self.page_ids:
            self.nav.select_row(self.nav.get_row_at_index(self.page_ids.index(arguments[0])))
        return 0

    def activate(self,*_):
        if self.get_active_window():
            self.window.present();return
        self.window=Adw.ApplicationWindow(application=self,title='Oh My Panasonic',default_width=1020,default_height=720)
        css=Gtk.CssProvider()
        css.load_from_data(b'''
            .pou-sidebar { padding: 16px 10px; background: alpha(currentColor,0.035); border-right: 1px solid alpha(currentColor,0.08); }
            .pou-sidebar row { border-radius: 10px; margin: 3px 0; padding: 7px 12px; min-height: 24px; }
            .pou-sidebar row:selected { background: alpha(@accent_color,0.18); color: @accent_color; }
            .pou-card { padding: 20px; border-radius: 16px; }
            .pou-reading { font-size: 29px; font-weight: 700; }
            .pou-page-title { font-size: 27px; font-weight: 700; }
            .pou-note { opacity: 0.72; font-size: 13px; }
            .pou-section { font-size: 16px; font-weight: 700; }
            .pou-metric { padding: 18px; border-radius: 14px; background: alpha(@accent_color,0.07); }
            .pou-action { min-height: 32px; min-width: 76px; padding: 5px 12px; border-radius: 9px; }
        ''')
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(),css,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        outer=box();outer.set_spacing(0)
        header=Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title='Oh My Panasonic',subtitle='Let’s Note 控制中心'))
        outer.append(header)
        body=box(False,0);body.set_vexpand(True);outer.append(body)
        sidebar=box();sidebar.add_css_class('pou-sidebar');sidebar.set_size_request(190,-1)
        sidebar.append(label('设备控制','pou-note'))
        self.nav=Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,css_classes=['navigation-sidebar'])
        sidebar.append(self.nav)
        nav_scroll=Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        nav_scroll.set_child(sidebar);body.append(nav_scroll)
        self.stack=Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,hexpand=True,vexpand=True)
        body.append(self.stack)
        self.message=label('正在连接系统服务…','pou-note')
        self.message.connect('notify::label',lambda item,_:item.set_visible(bool(item.get_label())))
        self.message.set_margin_start(24);self.message.set_margin_end(24);self.message.set_margin_top(10);self.message.set_margin_bottom(10)
        outer.append(self.message)
        pages=[('overview','总览','view-grid-symbolic'),('battery','电源与电池','battery-good-symbolic'),('cpu','电源策略','speedometer-symbolic'),('fan','风扇与散热','weather-windy-symbolic'),('network','网络与 Wi-Fi','network-wireless-symbolic'),('bluetooth','蓝牙设备','bluetooth-active-symbolic'),('audio','声音与设备','audio-volume-high-symbolic'),('display','显示与亮度','display-brightness-symbolic'),('touchpad','圆形触控板','input-touchpad-symbolic'),('storage','存储与节能','drive-harddisk-symbolic'),('face','人脸解锁','face-smile-symbolic')]
        pages.insert(-1,('indicator','状态栏','view-more-symbolic'))
        self.page_ids=[]
        for key,title,icon in pages:
            row=Gtk.ListBoxRow();line=box(False,10);line.append(Gtk.Image(icon_name=icon));line.append(label(title));row.set_child(line);self.nav.append(row);self.page_ids.append(key)
            scroll=Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            page=box(True,18)
            for side in ('start','end','top','bottom'):getattr(page,'set_margin_'+side)(26)
            page.append(label(title,'pou-page-title'))
            scroll.set_child(page);self.stack.add_named(scroll,key)
            start, first_card = len(self.updaters), len(self.cards)
            self.build_page(key,page)
            self.page_updates[key] = self.updaters[start:]
            self.page_cards[key] = self.cards[first_card:]
        self.nav.connect('row-selected',self.select_page)
        self.nav.select_row(self.nav.get_row_at_index(0))
        self.window.set_content(outer);self.window.present();self.update()
        GLib.timeout_add_seconds(5,self.update)

    def select_page(self, _, row):
        if row is None:return
        self.stack.set_visible_child_name(self.page_ids[row.get_index()])
        self.render_values()
        if self.window.get_visible():self.refresh_once()

    def get(self,*keys):
        value=self.data
        for key in keys:
            if not isinstance(value,dict):return None
            value=value.get(key)
        return value

    def battery(self,kind,key):
        b=(self.get('battery','batteries') or [{}])[0]
        value=b.get(kind,{}).get(key)
        return value.get('value') if isinstance(value,dict) else value

    def live(self,key):
        return None if self.get('power','stale') else self.get('power','sample',key)

    def mode(self):return MODES.get(self.get('power','mode'),'')

    def peripheral_controls(self,page):
        card=self.card(page,'无线与存储策略')
        self.stat(card,'当前策略',lambda:{'follow':'随 CPU 预设','system':'恢复系统','save':'省电','performance':'低延迟'}.get(self.get('peripherals','requested'),'等待服务'))
        self.actions(card,[(title,lambda m=mode:self.control('peripherals',m)) for mode,title in [('follow','随 CPU 预设'),('save','省电'),('performance','低延迟'),('system','恢复系统')]])
        self.text(card,lambda:self.get('peripherals','error') or '')

    def text(self,parent,getter,style='pou-note'):
        item=label('',style);parent.append(item)
        def update():
            raw=getter();value='' if raw is None else str(raw)
            item.set_label(value)
            item.set_visible(bool(value))
        self.updaters.append(update)
        return item

    def card(self,page,title):
        card=box();card.add_css_class('card');card.add_css_class('pou-card');page.append(card)
        self.cards.append(card)
        card.append(label(title,'pou-section'))
        return card

    def stat(self,parent,title,getter):
        row=box(False);name=label(title,'pou-note');name.set_hexpand(True);row.append(name)
        value=label();value.set_selectable(True);row.append(value);parent.append(row)
        def update():
            text = fmt(getter())
            row.set_visible(bool(text))
            value.set_label(text)
        self.updaters.append(update)

    def metrics(self,parent,items):
        row=box(False);parent.append(row)
        for title,getter in items:
            cell=box();cell.add_css_class('pou-metric');cell.set_hexpand(True)
            cell.append(label(title,'pou-note'));reading=label('', 'pou-reading');cell.append(reading);row.append(cell)
            def update(c=cell,r=reading,g=getter):
                value=fmt(g());r.set_label(value);c.set_visible(bool(value))
            self.updaters.append(update)
        self.updaters.append(lambda:row.set_visible(any(c.get_visible() for c in self.children(row))))

    @staticmethod
    def children(widget):
        child=widget.get_first_child()
        while child:
            yield child
            child=child.get_next_sibling()

    def actions(self,parent,items):
        if hasattr(Adw,'WrapBox'):
            flow=Adw.WrapBox(hexpand=True,child_spacing=8,line_spacing=8,
                             justify=Adw.JustifyMode.FILL,justify_last_line=True)
        else:
            flow=Gtk.FlowBox(hexpand=True,homogeneous=True,selection_mode=Gtk.SelectionMode.NONE,
                             max_children_per_line=min(3,len(items)),min_children_per_line=1,
                             column_spacing=8,row_spacing=8)
        parent.append(flow)
        sizes=Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        for title,callback in items:
            button=Gtk.Button(label=title() if callable(title) else title,hexpand=True,css_classes=['pou-action'])
            if callable(title):
                self.updaters.append(lambda b=button,getter=title:b.set_label(getter()))
            button.connect('clicked',lambda _,cb=callback:cb())
            sizes.add_widget(button)
            flow.append(button)

    def chart(self,parent,title,getter,maximum,unit):
        card=self.card(parent,title);area=Chart(maximum,unit);card.append(area)
        reading=label('', 'pou-note');card.append(reading)
        def update():
            points=[p for p in getter() if len(p)==2 and all(isinstance(n,(int,float)) and math.isfinite(n) for n in p)]
            changed = points != area.points
            area.points = points
            card.set_visible(bool(area.points))
            area.set_visible(len(area.points)>1)
            reading.set_label(f'最近 {area.points[-1][1]:.3g} {unit}' if area.points else '')
            if changed:area.queue_draw()
        self.updaters.append(update)

    def live_writer(self):
        pending = None
        running = False
        last_done = 0

        def submit(argv):
            nonlocal pending, running
            pending = argv
            if running:
                return
            def send():
                nonlocal pending, running
                current, pending, running = pending, None, True
                def finished(error):
                    nonlocal running, last_done
                    running = False
                    last_done = GLib.get_real_time() / 1e6
                    if error:
                        self.message.set_label(error[:400])
                    if pending is not None:
                        send()
                    else:
                        self.refresh_once()
                    return False
                def worker():
                    try:
                        result = subprocess.run(current, capture_output=True, text=True, timeout=45)
                        error = (result.stderr.strip() or '设置失败') if result.returncode else ''
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        error = str(exc)
                    GLib.idle_add(finished, error)
                threading.Thread(target=worker, daemon=True).start()
            send()
        return submit, lambda: running or (self.data.get('timestamp') or 0) < last_done

    def slider(self, parent, title, getter, argv, minimum=0, maximum=100, *, step=1, unit='%', command=None):
        row = box(False)
        name, reading = label(title, 'pou-note'), label()
        name.set_hexpand(True)
        row.append(name)
        row.append(reading)
        parent.append(row)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, minimum, maximum, step)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        parent.append(scale)
        self.scales[tuple(argv)] = scale
        submit, busy = self.live_writer()
        def change(widget):
            if self.updating:
                return
            value = round(widget.get_value() / step) * step
            reading.set_label(f'{value}{unit}')
            submit(command(value) if command else [*argv, str(value)])
        scale.connect('value-changed', change)
        def update():
            if busy():
                return
            value = getter()
            scale.set_sensitive(value is not None)
            row.set_visible(bool(fmt(value)))
            scale.set_visible(bool(fmt(value)))
            if value is not None:
                scale.set_value(value)
            reading.set_label(fmt(value, unit))
        self.updaters.append(update)
        return scale

    def brightness_control(self, parent):
        from .desktop import PANEL, PANEL_PATH
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        scale.set_sensitive(False)
        reading = label('正在读取亮度', 'pou-reading')
        parent.append(reading)
        parent.append(scale)
        bus = self.get_dbus_connection()
        pending = None
        writing = syncing = reading_now = False
        generation = 0

        def send():
            nonlocal pending, writing
            if pending is None or writing:
                return
            level, pending, writing = pending, None, True
            def done(conn, result):
                nonlocal writing
                writing = False
                try:
                    conn.call_finish(result)
                except GLib.Error as exc:
                    self.message.set_label(str(exc))
                send()
            bus.call(PANEL, PANEL_PATH, PANEL, 'SetBrightness', GLib.Variant('(i)', (level,)),
                     None, Gio.DBusCallFlags.NO_AUTO_START, 1000, None, done)

        def change(widget):
            nonlocal pending, generation
            if syncing:
                return
            generation += 1
            pending = round(widget.get_value())
            reading.set_label(f'{pending}%')
            send()

        def refresh():
            nonlocal reading_now
            if writing or reading_now:
                return
            requested = generation
            reading_now = True
            def done(conn, result):
                nonlocal syncing, reading_now
                reading_now = False
                try:
                    value, = conn.call_finish(result).unpack()
                except GLib.Error:
                    value = -1
                if requested != generation or writing:
                    return
                syncing = True
                try:
                    scale.set_sensitive(value >= 0)
                    scale.set_visible(value >= 0)
                    reading.set_visible(value >= 0)
                    if value >= 0:
                        scale.set_value(value)
                    reading.set_label(f'{value}%' if value >= 0 else '')
                finally:
                    syncing = False
            bus.call(PANEL, PANEL_PATH, PANEL, 'GetBrightness', None, None,
                     Gio.DBusCallFlags.NO_AUTO_START, 1000, None, done)

        scale.connect('value-changed', change)
        self.updaters.append(refresh)
        refresh()

    def ssd_controls(self,page):
        card=self.card(page,'SSD 电源')
        def value(key):
            return self.get('ssd_power',key) if self.get('ssd_power','available') and not self.get('ssd_power','stale') else None
        self.stat(card,'工作功率上限',lambda:fmt(value('maximum_w'),' W'))
        self.stat(card,'当前状态',lambda:{0:'工作 PS0',1:'工作 PS1',2:'工作 PS2',3:'空闲 PS3',4:'深度空闲 PS4'}.get(value('current_state'),'不可用'))
        self.stat(card,'自动空闲节能',lambda:'开启' if value('apst_enabled') else '关闭' if value('apst_enabled') is False else '不可用')
        self.stat(card,'策略',lambda:{'follow':'随电源模式','system':'系统管理','save':'省电','balanced':'均衡','performance':'性能'}.get(self.get('ssd_power','requested'),'等待服务'))
        self.actions(card,[(title,lambda m=mode:self.control('ssd-power',m)) for mode,title in [('follow','随电源模式'),('save','省电'),('balanced','均衡'),('performance','性能'),('system','系统管理')]])
        self.text(card,lambda:self.get('ssd_power','error') or '')
        self.updaters.append(lambda:card.set_visible(bool(self.get('ssd_power','available') or self.get('ssd_power','error'))))

    def devices(self,parent,kind,action):
        dropdown=Gtk.DropDown(hexpand=True)
        parent.append(dropdown)
        items=[]
        def selected(widget,_):
            if self.updating:return
            i=widget.get_selected()
            if i<len(items):self.execute(['/usr/bin/pou','audio',action,str(items[i]['id'])])
        dropdown.connect('notify::selected',selected)
        def update():
            nonlocal items
            devices=self.get('audio',kind) or []
            if [(d['id'],d['name']) for d in devices] != [(d['id'],d['name']) for d in items]:
                items=devices
                dropdown.set_model(Gtk.StringList.new([d['name'] for d in devices]))
            dropdown.set_sensitive(bool(devices))
            dropdown.set_visible(bool(devices))
            dropdown.set_selected(next((i for i,d in enumerate(devices) if d['default']),Gtk.INVALID_LIST_POSITION))
        self.updaters.append(update)

    def output(self,kind):return next((d for d in self.get('audio',kind) or [] if d.get('default')),{})

    def statusbar_controls(self,page):
        from . import statusbar
        icon_card=self.card(page,'图标')
        self.actions(icon_card,[('显示图标',lambda:self.execute(['/usr/bin/pou','tray'])),
                           ('隐藏图标',lambda:self.execute(['/usr/bin/pou','hide-tray']))])
        card=self.card(page,'常驻信息')
        count=label('', 'pou-note');card.append(count)
        grid=Gtk.FlowBox(hexpand=True,homogeneous=True,selection_mode=Gtk.SelectionMode.NONE,
                         max_children_per_line=2,min_children_per_line=1,column_spacing=18,row_spacing=12)
        card.append(grid)
        selected=statusbar.load();buttons={};changing=False
        icon=Gtk.CheckButton(label='触控板动态图标',active=statusbar.icon_enabled())
        def toggle_icon(button):
            try:statusbar.save(selected,touchpad_icon=button.get_active())
            except (OSError,ValueError) as exc:self.message.set_label(str(exc))
        icon.connect('toggled',toggle_icon)
        icon_card.append(icon)
        def update_choices():
            count.set_label(f'已选 {len(selected)} / {statusbar.MAX_ITEMS}')
            for key,button in buttons.items():
                button.set_sensitive(key in selected or len(selected)<statusbar.MAX_ITEMS)
        def changed(button,key):
            nonlocal selected,changing
            if changing:return
            proposed=[k for k,_ in statusbar.OPTIONS if (button.get_active() if k==key else k in selected)]
            try:statusbar.save(proposed)
            except (OSError,ValueError) as exc:
                changing=True;button.set_active(key in selected);changing=False
                self.message.set_label(str(exc));return
            selected=proposed;update_choices()
        for key,title in statusbar.OPTIONS:
            button=Gtk.CheckButton(label=title,active=key in selected,hexpand=True)
            button.connect('toggled',changed,key);buttons[key]=button;grid.append(button)
        update_choices()

    def build_page(self,key,page):
        if key=='overview':
            self.metrics(page,[('电池',lambda:fmt(self.battery('fields','capacity'),'%')),('CPU 功率',lambda:fmt(self.live('watts'),' W')),('CPU 温度',lambda:fmt(self.live('temperature'),' °C'))])
            card=self.card(page,'当前设备状态')
            self.stat(card,'网络',lambda:self.get('network','ssid') or '未连接')
            self.stat(card,'CPU 忙碌频率',lambda:fmt(self.live('busy_mhz'),' MHz'))
            self.stat(card,'内存使用',lambda:fmt(self.get('system','memory_percent'),'%'))
            self.stat(card,'电源策略',self.mode)
            self.stat(card,'内核',lambda:self.get('hardware','kernel') or '读取中')
            card=self.card(page,'状态栏')
            self.actions(card,[('显示图标',lambda:self.execute(['/usr/bin/pou','tray'])),('隐藏图标',lambda:self.execute(['/usr/bin/pou','hide-tray']))])
            self.actions(card,[('自定义显示内容',lambda:self.nav.select_row(self.nav.get_row_at_index(self.page_ids.index('indicator'))))])
            self.chart(page,'CPU 功率 · 最近 5 分钟 / W',lambda:[[p[0],p[1]] for p in self.get('power','history') or []],None,'W')
        elif key=='indicator':
            self.statusbar_controls(page)
        elif key=='battery':
            self.metrics(page,[('当前电量',lambda:fmt(self.battery('fields','capacity'),'%')),('供电来源',lambda:self.get('power_summary','source') or '读取中')])
            card=self.card(page,'供电接口与充电')
            self.stat(card,'电池状态',lambda:self.get('power_summary','status'))
            self.stat(card,'充电功率',lambda:self.get('power_summary','charge_power'))
            self.stat(card,'放电功率',lambda:self.get('power_summary','discharge_power'))
            self.stat(card,'预计拔电',lambda:self.get('care','departure_prediction','label'))
            self.stat(card,'PD 协商电压',lambda:self.get('battery','power_input','voltage_label'))
            self.stat(card,'实时输入功率',lambda:self.get('battery','power_input','input_label') if self.get('battery','power_input','source') in ('dc','typec','external') else None)
            self.stat(card,'PD 合约',lambda:self.get('battery','power_input','contract_details'))
            self.stat(card,'适配器档位',lambda:self.get('battery','power_input','profiles_label') if self.get('battery','power_input','ports') and self.get('battery','power_input','profiles_label')!='—' else None)
            card=self.card(page,'充电保养')
            self.updaters.append(lambda c=card:c.set_visible(bool(self.get('care','supported'))))
            self.stat(card,'服务状态',lambda:self.get('care','label') or '等待服务')
            self.slider(card,'充电上限',lambda:self.get('care','config','limit') if self.get('care','supported') else None,['/usr/bin/pkexec','/usr/libexec/pou-adjust','battery-limit'],50,100)
            self.actions(card,[('立刻充满',lambda:self.control('battery','full')),
                               (lambda:'使用软件电源管理' if self.get('care','config','mode')=='system' else '恢复系统电池管理',
                                lambda:self.control('battery','care' if self.get('care','config','mode')=='system' else 'system'))])
            self.text(card,lambda:self.get('care','error') or '')
            self.chart(page,'电量历史 · 最近 24 小时 / %',lambda:self.get('battery_history') or [],100,'%')
            self.chart(page,'电池充电功率 · 最近 1 小时 / W',lambda:self.get('charge_power_history') or [],None,'W')
            card=self.card(page,'容量与状态')
            self.stat(card,'累计充电指数',lambda:self.battery('metrics','charge_index') if self.battery('metrics','cycle_count') is None else None)
            self.stat(card,'电池循环次数',lambda:self.battery('metrics','cycle_count'))
            self.stat(card,'电池温度',lambda:fmt(self.battery('metrics','temperature_c'),' °C') if self.battery('metrics','temperature_c') is not None else None)
            for title,k,u in [('满充容量','energy_full_wh',' Wh'),('设计容量','energy_full_design_wh',' Wh'),('电池电压','voltage_v',' V')]:self.stat(card,title,lambda k=k,u=u:fmt(self.battery('metrics',k),u))
        elif key=='cpu':
            self.metrics(page,[('平台功耗',lambda:fmt(self.live('platform_w'),' W')),('封装功率',lambda:fmt(self.live('watts'),' W')),('忙碌频率',lambda:fmt(self.live('busy_mhz'),' MHz'))])
            card=self.card(page,'性能预设')
            self.updaters.append(lambda c=card:c.set_visible(bool(self.get('power','presets'))))
            self.stat(card,'当前预设',self.mode)
            self.stat(card,'长期功率上限',lambda:fmt(self.get('power','pl1_w'),' W'))
            self.stat(card,'短时功率上限',lambda:fmt(self.get('power','pl2_w'),' W'))
            from .power import model_presets
            presets=model_presets()
            names={'eco':'省电','balanced':'均衡','performance':'性能','adaptive':'温控性能','boost':'增强性能'}
            self.actions(card,[(f"{names[mode]} {preset['pl1']} W",lambda m=mode:self.control('cpu',m)) for mode,preset in presets.items()]+[('系统管理',lambda:self.control('cpu','system'))])
            system_card=self.card(page,'系统电源模式')
            self.stat(system_card,'当前',lambda:{'power-saver':'省电','balanced':'均衡','performance':'性能'}.get(self.get('system','profile')))
            self.actions(system_card,[(title,lambda m=mode:self.control('profile',m)) for mode,title in [('power-saver','省电'),('balanced','均衡'),('performance','性能')]])
            self.text(card,lambda:self.get('power','error') or '')
            self.peripheral_controls(page)
            self.chart(page,'平台功耗 · 最近 5 分钟 / W',lambda:[[p[0],p[4]] for p in self.get('power','history') or [] if len(p)>4 and p[4] is not None],None,'W')
            self.chart(page,'封装功率 · 最近 5 分钟 / W',lambda:[[p[0],p[1]] for p in self.get('power','history') or []],None,'W')
            self.chart(page,'忙碌频率 · 最近 5 分钟 / MHz',lambda:[[p[0],p[2]] for p in self.get('power','history') or []],None,'MHz')
            card=self.card(page,'CPU 统计')
            self.stat(card,'含空闲时间的平均频率',lambda:fmt(self.live('average_mhz'),' MHz'))
            self.stat(card,'CPU 活跃时间比例',lambda:fmt(self.live('busy_percent'),'%'))
        elif key=='fan':
            from .fan_page import build
            build(self,page)
        elif key=='network':
            self.metrics(page,[('Wi-Fi 信号',lambda:fmt(self.get('network','signal'),'%')),('链路速率',lambda:fmt(self.get('network','rate_mbps'),' Mbps'))])
            card=self.card(page,'无线连接')
            for title,k in [('当前网络','ssid'),('网关','gateway'),('频率 MHz','frequency')]:self.stat(card,title,lambda k=k:fmt(self.get('network',k)))
            self.stat(card,'IPv4',lambda:', '.join(self.get('network','addresses') or []) or '未分配')
            self.stat(card,'无线省电',lambda:'、'.join(f"{d['name']}：{'开启' if d['value']=='on' else '关闭'}" for d in self.get('peripherals','devices') or [] if d['kind']=='wifi') or '接口未提供')
            self.actions(card,[('开启 Wi-Fi',lambda:self.control('wifi','on')),('关闭 Wi-Fi',lambda:self.control('wifi','off')),('更多网络设置',lambda:self.settings('wifi')),('有线 / VPN',lambda:self.settings('network'))])
            from .wifi_page import Networks
            self.wifi_networks=Networks(self,self.card(page,'附近网络'))
            self.chart(page,'下载速率 · 最近 5 分钟 / KB/s',lambda:self.rx_points,None,'KB/s')
            self.chart(page,'上传速率 · 最近 5 分钟 / KB/s',lambda:self.tx_points,None,'KB/s')
        elif key=='bluetooth':
            card=self.card(page,'蓝牙连接')
            self.stat(card,'无线状态',lambda:'已开启' if self.get('bluetooth','enabled') else '已关闭')
            self.actions(card,[('开启蓝牙',lambda:self.control('bluetooth','on')),('关闭蓝牙',lambda:self.control('bluetooth','off')),('系统蓝牙设置',lambda:self.settings('bluetooth'))])
            from .bluetooth_page import Devices
            self.bluetooth_devices=Devices(self,self.card(page,'附近与已配对设备'))
        elif key=='audio':
            for kind,title,action,mute in [('sinks','扬声器 / 输出','volume','mute'),('sources','麦克风 / 输入','mic-volume','mic-mute')]:
                card=self.card(page,title)
                card.set_spacing(8)
                self.devices(card,kind,'output' if kind=='sinks' else 'input')
                target = ['set-sink-volume','@DEFAULT_SINK@'] if action=='volume' else ['set-source-volume','@DEFAULT_SOURCE@']
                self.slider(card,'音量',lambda k=kind:self.output(k).get('volume'),target,
                            command=lambda v,t=target:['/usr/bin/pactl',*t,f'{v}%'])
                self.stat(card,'静音',lambda k=kind:('已静音' if self.output(k).get('mute') else '未静音') if self.output(k) else None)
                self.actions(card,[('切换静音',lambda m=mute:self.execute(['/usr/bin/pou','audio',m,'toggle']))])
                self.updaters.append(lambda c=card,k=kind:c.set_visible(bool(self.output(k))))
            card=self.card(page,'音频设备');self.actions(card,[('选择输入 / 输出设备',lambda:self.settings('sound'))])
        elif key=='display':
            card=self.card(page,'内置显示屏');self.brightness_control(card)
            row=box(False); title=label('自动亮度'); title.set_hexpand(True); row.append(title)
            automatic=Gtk.Switch(valign=Gtk.Align.CENTER); row.append(automatic); card.append(row)
            self.display_settings=Gio.Settings.new('org.gnome.settings-daemon.plugins.power')
            self.display_settings.bind('ambient-enabled',automatic,'active',Gio.SettingsBindFlags.DEFAULT)
            from .display_page import build as display_controls
            display_controls(self,page)
            from .resolution_page import Displays
            self.displays=Displays(self,self.card(page,'分辨率与缩放'))
            self.actions(card,[('排列与更多显示设置',lambda:self.settings('display'))])
        elif key=='face':
            from .face_page import Enrollment
            card=self.card(page,'锁屏解锁')
            self.stat(card,'状态',lambda:'已启用' if self.get('face','enabled') else '未启用' if self.get('face','available') else '识别组件未安装')
            def manage(action):
                self.execute(['/usr/bin/pkexec','/usr/libexec/pou-face-manage',action])
                if action in ('enroll','enable'):
                    self.message.set_label('请完成账户认证，然后正对红外摄像头。')
            def confirm_forget():
                dialog=Adw.MessageDialog(transient_for=self.window,heading='删除本账户的人脸录入？')
                dialog.add_response('cancel','取消')
                dialog.add_response('forget','删除')
                dialog.set_default_response('cancel')
                dialog.connect('response',lambda _,response:manage('forget') if response=='forget' else None)
                dialog.present()
            self.enrollment=Enrollment(self,card)
            toggle=Gtk.Button(hexpand=True)
            toggle.connect('clicked',lambda *_:manage('disable' if self.get('face','enabled') else 'enable'))
            card.append(toggle)
            def update_face():
                active=self.get('face','enabled')
                self.enrollment.start_button.set_label('补录姿态与光照' if active else '查看取景' if self.get('face','enrolled') else '录入并启用')
                toggle.set_label('停用' if active else '重新启用')
                toggle.set_visible(bool(active or self.get('face','enrolled')))
                toggle.set_sensitive(not self.enrollment.panel.get_visible() and self.enrollment.worker is None)
            original=Gtk.Button(label='恢复原始姿态',hexpand=True,visible=False)
            original.connect('clicked',lambda *_:manage('restore-poses'))
            card.append(original)
            self.updaters.append(lambda:original.set_visible(bool(self.get('face','supplemented'))))
            self.updaters.append(update_face)
            self.actions(card,[('删除录入',confirm_forget)])
            card=self.card(page,'刷脸授权')
            def opted(name):
                return name in (self.get('face','privileges') or [])
            for service,title in [('polkit-1','桌面权限弹窗'),('sudo','本地终端 sudo')]:
                button=Gtk.Button(hexpand=True)
                def refresh_privilege(b=button,name=service,label=title):
                    b.set_label(f'{label} · ' + ('已开启' if opted(name) else '已关闭'))
                    b.set_sensitive(bool(self.get('face','enabled')))
                button.connect('clicked',lambda _,name=service:manage(name+('-off' if opted(name) else '-on')))
                card.append(button)
                self.updaters.append(refresh_privilege)
        elif key=='touchpad':
            card=self.card(page,'圆形滚动')
            self.stat(card,'WheelPad 服务',lambda:'正在运行' if self.get('system','pad_active') else '原生触控板')
            self.actions(card,[('启用圆形滚动',lambda:self.control('enable')),('恢复原生触控板',lambda:self.control('disable'))])
            card=self.card(page,'手感调整')
            def preferences():
                try:
                    return json.loads(Path('/etc/panasonic-on-ubuntu/wheelpad.json').read_text())
                except (OSError, ValueError):
                    return {}
            base=['/usr/bin/pkexec','/usr/libexec/pou-adjust','configure']
            natural=Gtk.Switch(active=preferences().get('natural',True),halign=Gtk.Align.START)
            sensitivity=self.slider(card,'滚动灵敏度',lambda:preferences().get('sensitivity',0),base,-2,2,
                                    unit='',command=lambda v:[*base,str(v),str(int(natural.get_active()))])
            card.append(label('自然滚动方向','pou-note'))
            card.append(natural)
            natural.connect('notify::active',lambda *_:sensitivity.emit('value-changed'))
            self.actions(card,[('指针 / 轻触 / 手势',lambda:self.settings('mouse'))])
        if key=='cpu':
            self.ssd_controls(page)
            card=self.card(page,'内置光驱')
            row=box(False,24);card.append(row)
            emblem=box();emblem.set_valign(Gtk.Align.CENTER);row.append(emblem)
            emblem.append(Gtk.Image(icon_name='media-optical-symbolic',pixel_size=64))
            self.text(emblem,lambda:'已供电' if self.get('optical','powered') else '已断电' if self.get('optical','supported') else '不可用','pou-section')
            details=box();details.set_hexpand(True);row.append(details)
            self.text(details,lambda:self.get('optical','model') or '','pou-section')
            self.text(details,lambda:self.get('optical','details','capabilities') or '')
            for title,field in [('光碟','media'),('光碟属性','attributes'),('光碟容量','capacity'),('已用空间','used'),('读写','access')]:
                self.stat(details,title,lambda f=field:self.get('optical','details',f))
            self.stat(card,'启动策略',lambda:'保持供电' if self.get('optical','policy')=='on' else '空驱动器自动省电')
            self.actions(card,[('开启并保持供电',lambda:self.control('optical','on')),('关闭并自动省电',lambda:self.control('optical','auto'))])
            self.text(card,lambda:self.get('optical','error') or '')
            self.updaters.append(lambda:card.set_visible(bool(self.get('optical','supported'))))
        elif key=='storage':
            self.metrics(page,[('已用空间',lambda:fmt(self.get('hardware','root_used_gb'),' GB')),('总容量',lambda:fmt(self.get('hardware','root_total_gb'),' GB'))])
            card=self.card(page,'NVMe 空闲电源管理')
            self.text(card,lambda:'\n\n'.join(f"{d.get('model') or d['name']}\n允许唤醒延迟：{d['value']} µs" for d in self.get('peripherals','devices') or [] if d['kind']=='nvme'))
            self.updaters.append(lambda:card.set_visible(any(d['kind']=='nvme' for d in self.get('peripherals','devices') or [])))
            self.peripheral_controls(page)
            self.ssd_controls(page)

    def launch(self,argv):
        try:subprocess.Popen(argv)
        except OSError as exc:self.message.set_label(str(exc))
    def settings(self,page):self.launch(['gnome-control-center',page])
    def control(self,*args):
        helper = '/usr/libexec/pou-adjust' if args[:1] == ('battery',) and args[1:] in [('full',),('system',),('care',)] else '/usr/libexec/pou-control'
        self.execute(['pkexec',helper,*args])
    def execute(self,argv,on_done=None):
        self.message.set_label('正在应用设置…')
        def finished(message,power):
            if power and power.get('timestamp',0)>=(self.get('power','timestamp') or 0):
                self.data['power']=power
                self.render_values()
            self.message.set_label(message[:400]);self.refresh_once()
            if on_done:on_done()
            return False
        def work():
            power=None
            try:
                result=subprocess.run(argv,capture_output=True,text=True,timeout=120)
                message='设置已应用。' if result.returncode==0 else result.stderr.strip() or '操作未完成。'
                if result.returncode==0 and argv[1:3]==['/usr/libexec/pou-control','cpu'] and result.stdout.strip():
                    power=json.loads(result.stdout)
            except Exception as exc:message=str(exc)
            GLib.idle_add(finished,message,power)
        threading.Thread(target=work,daemon=True).start()
    def refresh_once(self):self.update(force=True);return False
    def update(self, force=False):
        if not self.get_active_window():return False
        surface = self.window.get_surface()
        if not force and surface and surface.get_state() & Gdk.ToplevelState.MINIMIZED:return True
        if self.loading:
            if force:self.refresh_pending=True
            return True
        scope = self.stack.get_visible_child_name()
        if scope=='indicator':
            if self.message.get_label()=='正在连接系统服务…':self.message.set_label('')
            return True
        self.loading=True
        def work():
            try:
                from .telemetry import collect
                value=collect(scope)
                GLib.idle_add(self.apply,value)
            except Exception as exc:GLib.idle_add(self.failed,str(exc))
        threading.Thread(target=work,daemon=True).start();return True
    def failed(self,error):
        self.loading=False;self.message.set_label('状态刷新失败：'+error[:250])
        if self.refresh_pending:
            self.refresh_pending=False;self.refresh_once()
        return False
    def apply(self,value):
        if value.get('scope',self.stack.get_visible_child_name()) not in ('all',self.stack.get_visible_child_name()):
            self.loading=False;self.refresh_pending=False;self.refresh_once();return False
        network=value.get('network',{})
        current=(value.get('timestamp',0),network.get('interface'),network.get('rx_bytes'),network.get('tx_bytes'))
        previous=self.net_previous
        if previous and current[1]==previous[1] and current[0]>previous[0] and all(v is not None for v in current[2:]+previous[2:]):
            if current[2]>=previous[2] and current[3]>=previous[3]:
                elapsed=current[0]-previous[0]
                self.rx_points.append([current[0],(current[2]-previous[2])/elapsed/1000])
                self.tx_points.append([current[0],(current[3]-previous[3])/elapsed/1000])
        elif previous and current[1]!=previous[1]:
            self.rx_points,self.tx_points=[],[]
        self.net_previous=current
        self.rx_points=[p for p in self.rx_points if p[0]>=current[0]-300]
        self.tx_points=[p for p in self.tx_points if p[0]>=current[0]-300]
        old=self.data.get('power',{})
        stamp=value.get('power',{}).get('timestamp')
        if stamp is not None and stamp<old.get('timestamp',0):
            value['power']=old
        self.data=value;self.loading=False
        self.render_values()
        if self.message.get_label()=='正在连接系统服务…':self.message.set_label('')
        if self.refresh_pending:
            self.refresh_pending=False;self.refresh_once()
        return False

    def render_values(self):
        self.updating=True
        try:
            key=self.stack.get_visible_child_name()
            cards=self.page_cards.get(key,[])
            for card in cards:card.set_visible(True)
            for update in self.page_updates.get(key,[]):update()
            for card in cards:
                if not any(c.get_visible() for c in list(self.children(card))[1:]):card.set_visible(False)
        finally:self.updating=False


def run(page=None):App().run(['pou']+([page] if page else []))
