import St from 'gi://St';
import {fmt} from './readings.js';
import {FaceConsent} from './face.js';
import {CompactStatus} from './statusbar.js';
import {TouchpadIndicator} from './touchpad.js';
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as SystemActions from 'resource:///org/gnome/shell/misc/systemActions.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as Slider from 'resource:///org/gnome/shell/ui/slider.js';

const BUS = 'io.github.panasonic_on_ubuntu.Panel';
const XML = `<node><interface name="${BUS}"><method name="Show"/><method name="Hide"/><method name="Open"><arg name="page" type="s" direction="in"/></method><method name="SetBrightness"><arg name="percent" type="i" direction="in"/></method><method name="GetBrightness"><arg name="percent" type="i" direction="out"/></method><method name="Inspect"><arg name="result" type="s" direction="out"/></method></interface></node>`;
const box = (vertical = true, style = '') => new St.BoxLayout({vertical, style_class: style, x_expand: true});
const label = (text, style = '') => new St.Label({text: String(text), style_class: style, y_align: Clutter.ActorAlign.CENTER});
const modes = {'balanced': '均衡', 'power-saver': '省电', 'performance': '性能'};
const fanName = value => ({0: '标准自动曲线', 1: '静音自动曲线'}[value] ?? '不可用');
const fanPolicy = data => ({standard:'标准',quiet:'静音',performance:'性能曲线',custom:'自定义曲线',manual:'手动输出'}[data.power?.fan?.mode] ?? fanName(data.firmware?.fan_profile));
const cpuModes = {system:'系统管理',eco:'极致省电',balanced:'日常均衡',performance:'性能',adaptive:'温控性能',boost:'增强性能'};
function axisMax(series, fixed) {
    const peak = series.reduce((max, p) => Math.max(max, p[1]), 0);
    if (fixed && peak <= fixed) return fixed;
    if (peak <= 0) return 1;
    const step = 10 ** Math.floor(Math.log10(peak));
    return ([1, 2, 4, 5, 8, 10].find(n => n * step >= peak) ?? 10) * step;
}
const formatTick = n => String(Number(n.toPrecision(3)));
const signal = n => n >= 75 ? '▂▄▆█' : n >= 50 ? '▂▄▆' : n >= 25 ? '▂▄' : '▂';

function createPlot(maximum = null, meter = false) {
    const area = new St.DrawingArea({style_class: meter ? 'pou-meter' : 'pou-chart', x_expand: true});
    area.points = [];
    area.second = [];
    area.maximum = maximum;
    area.meter = meter;
    area.connect('repaint', paintPlot);
    return area;
}

function paintPlot(area) {
    const cr = area.get_context();
    const [w, h] = area.get_surface_size();
    if (w <= 0 || h <= 0) { cr.$dispose(); return; }
    cr.setSourceRGBA(.5, .5, .5, .16);
    if (area.meter) {
        cr.rectangle(0, 0, w, h); cr.fill();
        cr.setSourceRGBA(.36, .8, .69, 1);
        cr.rectangle(0, 0, w * Math.min(1, Math.max(0, area.points[0]?.[1] ?? 0)/100), h); cr.fill();
    } else {
        cr.moveTo(1,4); cr.lineTo(1,h-4); cr.stroke();
        for (const y of [4, h/2, h-4]) { cr.moveTo(0, y); cr.lineTo(w, y); cr.stroke(); }
        const all = [...area.points, ...area.second];
        if (all.length) {
            const start = Math.min(...all.map(p => p[0])); const end = Math.max(start+1, ...all.map(p => p[0]));
            const maximum = area.maximum;
            for (const [index, series] of [area.points, area.second].entries()) {
                cr.setSourceRGBA(...(index ? [.49,.63,.98,1] : [.36,.8,.69,1])); cr.setLineWidth(2);
                series.forEach(([t, v], i) => { const x=(t-start)/(end-start)*w; const y=4+(h-8)*(1-Math.max(0,Math.min(maximum,v))/maximum); if (i) cr.lineTo(x,y); else cr.moveTo(x,y); });
                cr.stroke();
            }
        }
    }
    cr.$dispose();
}

export default class Panasonic extends Extension {
    enable() {
        this.modeSignal = Main.sessionMode.connect('updated', () => this.syncMode());
        this.syncMode();
    }
    syncMode() {
        const mode = Main.sessionMode.isGreeter ? 'none' : Main.sessionMode.isLocked ? 'locked' : 'user';
        if (mode === this.mode) return;
        this.leaveMode();
        this.mode = mode;
        if (mode === 'user') this.enableControls();
        try {
            if (mode === 'user') this.faceSurface = new FaceConsent();
        } catch (e) { console.error(e); }
    }
    leaveMode() {
        this.faceSurface?.destroy();
        this.faceSurface = null;
        if (this.mode === 'user') this.disableControls();
        this.mode = null;
    }
    disable() {
        if (this.modeSignal) Main.sessionMode.disconnect(this.modeSignal);
        this.modeSignal = 0;
        this.leaveMode();
    }
    enableControls() {
        this.displaySettings = new Gio.Settings({schema_id:'org.gnome.settings-daemon.plugins.power'});
        this.alive = true; this.hidden = false; this.data = {}; this.previous = null; this.updaters = [];
        this.loading = false; this.refreshPending = false; this.timer = 0;
        this.cpuPoints = []; this.rxPoints = []; this.txPoints = []; this.sources = new Set(); this.processes = new Set();
        this.button = new PanelMenu.Button(0, 'Oh My Panasonic');
        const top = box(false, 'pou-panel-box');
        this.touchpadIndicator=new TouchpadIndicator(()=>this.syncIndicator());
        top.add_child(this.touchpadIndicator.area);
        this.percent = label(''); top.add_child(this.percent); this.button.add_child(top);
        this.compact = new CompactStatus(this.percent,enabled=>{
            this.touchpadIconEnabled=enabled;this.touchpadIndicator.setEnabled(enabled && !this.hidden);
        },()=>this.syncIndicator());
        Main.panel.addToStatusArea(this.uuid, this.button);
        if (GLib.file_test(GLib.build_filenamev([GLib.get_user_config_dir(),'panasonic-on-ubuntu','indicator-hidden']),GLib.FileTest.EXISTS))
            this.hide();
        const root = new PopupMenu.PopupBaseMenuItem({reactive: true, activate: false, hover: false, can_focus: false});
        this.scroll = new St.ScrollView({style_class: 'pou-scroll', overlay_scrollbars: true, x_expand: true});
        this.body = box(true, 'pou-body'); this.scroll.set_child(this.body); root.add_child(this.scroll); this.button.menu.addMenuItem(root);
        this.button.menu.connect('open-state-changed', (_, open) => {
            if (this.timer) GLib.Source.remove(this.timer);
            this.timer = 0;
            if (open) {
                this.refresh();
                this.timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 5, () => {
                    this.refresh(); return GLib.SOURCE_CONTINUE;
                });
            } else this.stopTelemetry();
        });
        this.dbus = Gio.DBusExportedObject.wrapJSObject(XML, {Show: () => this.show(), Hide: () => this.hide(), Open: page => {this.show(); this.page(page); this.button.menu.open();}, SetBrightness: p => this.setScreenPercent(p), GetBrightness: () => this.screenPercent() ?? -1, Inspect: () => JSON.stringify({ready: !!this.data.timestamp, page: this.route, visible: !this.hidden, errors: this.data.errors ?? {}, readout: this.percent.text, readoutItems: this.compact?.items ?? [], touchpadActive: this.touchpadIndicator?.area.visible ?? false, version: '0.22.0', sections: ['battery','network','bluetooth','audio','power','touchpad','display']})});
        this.dbus.export(Gio.DBus.session, '/io/github/panasonic_on_ubuntu/Panel');
        this.owner = Gio.bus_own_name_on_connection(Gio.DBus.session, BUS, Gio.BusNameOwnerFlags.NONE, null, null);
        this.page('home'); this.watchBattery();
    }
    watchBattery() {
        const cancelled = new Gio.Cancellable();
        this.batteryCancel = cancelled;
        Gio.DBusProxy.new_for_bus(Gio.BusType.SYSTEM, Gio.DBusProxyFlags.DO_NOT_AUTO_START, null,
            'org.freedesktop.UPower', '/org/freedesktop/UPower/devices/DisplayDevice',
            'org.freedesktop.UPower.Device', cancelled, (_, result) => {
                if (!this.alive || cancelled.is_cancelled()) return;
                try {
                    this.batteryProxy = Gio.DBusProxy.new_for_bus_finish(result);
                    const update = () => {
                        this.compact.updateBattery(this.batteryProxy);
                    };
                    this.batteryChanged = this.batteryProxy.connect('g-properties-changed', update);
                    this.batteryOwner = this.batteryProxy.connect('notify::g-name-owner', update);
                    update();
                } catch (e) { if (!cancelled.is_cancelled()) console.error(e); }
            });
    }
    syncIndicator() { if(this.alive && this.button) this.button.visible=!this.hidden && (this.percent.visible || this.touchpadIndicator?.area.visible); }
    show() { this.hidden = false; this.compact?.setVisible(true);this.touchpadIndicator?.setEnabled(this.touchpadIconEnabled!==false);this.syncIndicator(); }
    hide() { this.hidden = true; this.button.menu.close();this.compact?.setVisible(false);this.touchpadIndicator?.setEnabled(false);this.button.hide(); }
    stopTelemetry() {
        const worker = this.telemetryWorker;
        this.telemetryWorker = null;
        if (!worker) return;
        worker.cancel.cancel();
        worker.process.force_exit();
        this.processes.delete(worker.process);
        if (!worker.busy) this.closeTelemetryPipes(worker);
    }
    closeTelemetryPipes(worker) {
        for (const stream of [worker.input, worker.output]) {
            try { stream.close(null); } catch (_) { /* Cancellation already released the reader. */ }
        }
    }
    async collectTelemetry(scope) {
        if (!this.telemetryWorker) {
            const process = Gio.Subprocess.new(['/usr/bin/pou','telemetry-stream'],
                Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
            this.processes.add(process);
            this.telemetryWorker = {process, cancel:new Gio.Cancellable(),
                input:process.get_stdin_pipe(), output:new Gio.DataInputStream({base_stream:process.get_stdout_pipe()})};
        }
        const worker = this.telemetryWorker;
        worker.busy = true;
        const timeout = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 20, () => {
            this.sources.delete(timeout);
            if (this.telemetryWorker === worker) this.stopTelemetry();
            return GLib.SOURCE_REMOVE;
        });
        this.sources.add(timeout);
        try {
            await new Promise((resolve,reject) => worker.input.write_all_async(new TextEncoder().encode(scope+'\n'),
                GLib.PRIORITY_DEFAULT, worker.cancel, (stream,result) => {
                    try { stream.write_all_finish(result);resolve(); } catch(e) { reject(e); }
                }));
            return await new Promise((resolve,reject) => worker.output.read_line_async(GLib.PRIORITY_DEFAULT,
                worker.cancel,(stream,result) => {
                    try {
                        const [line] = stream.read_line_finish_utf8(result);
                        if (line === null) throw new Error('状态采集连接已关闭');
                        resolve(line);
                    } catch(e) { reject(e); }
                }));
        } catch(e) {
            if (this.telemetryWorker === worker) this.stopTelemetry();
            throw e;
        } finally {
            worker.busy = false;
            if (this.telemetryWorker !== worker) this.closeTelemetryPipes(worker);
            if (this.sources.delete(timeout)) GLib.Source.remove(timeout);
        }
    }
    async run(argv) {
        const process = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        this.processes.add(process);
        return await new Promise((resolve, reject) => {
            const timeout = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, argv.includes('pkexec') || argv.includes('/usr/bin/pkexec') ? 120 : 20, () => { this.sources.delete(timeout); process.force_exit(); return GLib.SOURCE_REMOVE; });
            this.sources.add(timeout);
            process.communicate_utf8_async(null, null, (proc, result) => {
                if (this.sources.delete(timeout)) GLib.Source.remove(timeout);
                this.processes.delete(process);
                try { const [, stdout, stderr] = proc.communicate_utf8_finish(result); if (!proc.get_successful()) throw new Error(stderr.trim() || stdout.trim() || '操作未完成'); resolve(stdout); } catch (e) { reject(e); }
            });
        });
    }
    async refresh() {
        if (!this.alive || !this.button.menu.isOpen) return;
        if (this.loading) { this.refreshPending = true; return; }
        this.loading = true;
        const scope = this.route;
        try {
            const data = JSON.parse(await this.collectTelemetry(scope));
            if (!this.alive) return;
            if (!this.button.menu.isOpen) return;
            if (scope !== this.route) { this.refreshPending = true; return; }
            const old = this.previous;
            if (old && data.timestamp > old.timestamp) {
                const elapsed = data.timestamp-old.timestamp;
                const total = (data.system?.cpu_total ?? 0)-(old.system?.cpu_total ?? 0);
                if (total > 0 && data.system && old.system) this.cpuPoints.push([data.timestamp, Math.max(0,Math.min(100,100*(1-((data.system.cpu_idle-old.system.cpu_idle)/total))))]);
                for (const [key, points] of [['rx_bytes',this.rxPoints],['tx_bytes',this.txPoints]]) {
                    const now = data.network?.[key]; const prior = old.network?.[key];
                    if (now != null && prior != null && now >= prior && data.network.interface === old.network.interface) points.push([data.timestamp,(now-prior)/elapsed/1024]);
                }
                for (const points of [this.cpuPoints,this.rxPoints,this.txPoints]) while(points.length && points[0][0] < data.timestamp-300) points.shift();
            }
            if (data.power?.timestamp != null && data.power.timestamp < this.data.power?.timestamp)
                data.power = this.data.power;
            this.previous = data; this.data = data;
            this.update();
        } catch(e) { if (this.alive && this.button.menu.isOpen) this.message(`状态刷新失败：${e.message}`); }
        finally {
            this.loading = false;
            if (this.refreshPending) { this.refreshPending = false; this.refresh(); }
        }
    }
    battery() { return this.data.battery?.batteries?.[0]; }
    value(key) { return this.battery()?.fields?.[key]?.value; }
    metric(key) { return this.battery()?.metrics?.[key]; }
    update() { this.updating = true; for(const rows of this.groups??[])rows.forEach(r=>r.show()); for (const update of this.updaters) update(); this.updating = false; }
    message(text) { if(this.errorLabel) { this.errorLabel.text = text.slice(0,250); this.errorLabel.visible = !!text; } }
    async action(argv) {
        this.message('正在应用…');
        try {
            const output = await this.run(argv);
            if (!this.alive) return;
            if (argv[1] === '/usr/libexec/pou-control' && argv[2] === 'cpu' && output.trim()) {
                const power = JSON.parse(output);
                if (power.timestamp >= (this.data.power?.timestamp ?? 0)) this.data.power = power;
                this.update();
            }
            this.message('已应用'); await this.refresh();
        }
        catch(e) { if(this.alive) {this.message(e.message); Main.notifyError('Oh My Panasonic：操作未完成', e.message.slice(0,400));} }
    }
    control(...args) {
        const helper = args[0] === 'battery' && ['full','system','care'].includes(args[1]) ? '/usr/libexec/pou-adjust' : '/usr/libexec/pou-control';
        this.action(['/usr/bin/pkexec',helper,...args]);
    }
    settings(panel) { this.button.menu.close(); this.launch(['gnome-control-center',panel]); }
    bind(widget, getter) { this.updaters.push(() => { widget.text = String(getter() ?? ''); widget.visible = !!widget.text; }); return widget; }
    text(getter, style = 'pou-subtitle', parent = this.body) { const item = this.bind(label('',style),getter); item.clutter_text.line_wrap=true;item.clutter_text.ellipsize=Pango.EllipsizeMode.NONE;parent.add_child(item); return item; }
    stat(title, getter, parent = this.body) { const row=box(false,'pou-row'); const name=label(title,'pou-key'); name.x_expand=true; row.add_child(name); const value=this.bind(label('','pou-value'),()=>fmt(getter())); row.add_child(value); parent.add_child(row); this.updaters.push(()=>{row.visible=!!value.text;}); }
    actionButton(title, action, parent=this.body, dynamic=null) { const btn=new St.Button({label:title,style_class:'pou-button',can_focus:true,x_expand:true}); btn.connect('clicked',action); parent.add_child(btn); if(dynamic) this.updaters.push(()=>{btn.label=dynamic();}); return btn; }
    nav(title, icon, detail, page) { const btn=new St.Button({style_class:'pou-row',can_focus:true,x_expand:true}); const row=box(false); row.add_child(new St.Icon({icon_name:icon,icon_size:20})); const texts=box(); texts.add_child(label(title,'pou-value')); texts.add_child(this.bind(label('','pou-subtitle'),()=>fmt(detail()))); row.add_child(texts); row.add_child(new St.Icon({icon_name:'go-next-symbolic',icon_size:16})); btn.set_child(row); btn.connect('clicked',()=>this.page(page)); this.body.add_child(btn); }
    chart(title, getter, unit, max=null, second=null) {
        const heading=this.text(()=>title);
        const latest = label('', 'pou-value');
        this.body.add_child(latest);
        const row = box(false);
        const axis = new St.BoxLayout({vertical:true, style_class:'pou-chart-axis'});
        const ticks = [];
        for (let i = 0; i < 3; i++) {
            if (i) axis.add_child(new St.Widget({y_expand:true}));
            const tick = label('', 'pou-axis-label');
            ticks.push(tick); axis.add_child(tick);
        }
        const area = createPlot();
        row.add_child(axis); row.add_child(area); this.body.add_child(row);
        let previousPoints = '';
        this.updaters.push(()=>{
            const valid = p => Array.isArray(p) && p.length >= 2 && p.every(Number.isFinite);
            area.points = getter().filter(valid);
            area.second = second ? second().filter(valid) : [];
            const hasData=area.points.length>0 || area.second.length>0;
            heading.visible=hasData;latest.visible=hasData;
            row.visible=area.points.length>1 || area.second.length>1;
            area.maximum = axisMax([...area.points,...area.second], max);
            ticks.forEach((tick,i) => {tick.text = formatTick(area.maximum*(1-i/2));});
            const reading = series => series.length ? formatTick(series.at(-1)[1]) + ' ' + unit : '—';
            latest.text = second ? [area.points.length?'下载 '+reading(area.points):'',area.second.length?'上传 '+reading(area.second):''].filter(Boolean).join('　')
                                 : area.points.length ? '最近 ' + reading(area.points) : '';
            const signature = JSON.stringify([area.points, area.second]);
            if (signature !== previousPoints) { previousPoints = signature; area.queue_repaint(); }
        });
    }
    meter(getter, parent=this.body) { const area=createPlot(100,true); parent.add_child(area); this.updaters.push(()=>{const value=getter();area.visible=Number.isFinite(value);area.points=area.visible?[[0,value]]:[];area.queue_repaint();}); }
    buttons(items) { const row=box(false,'pou-actions'); this.body.add_child(row); for(const [title,callback,dynamic] of items)this.actionButton(title,callback,row,dynamic); }
    list(getter, render) { const target=box();this.body.add_child(target);let previous='';this.updaters.push(()=>{const items=getter();const signature=JSON.stringify(items);if(signature===previous)return;previous=signature;target.destroy_all_children();if(!items.length)target.add_child(label('暂无设备或记录','pou-subtitle'));for(const item of items)render(item,target);}); }
    slider(title, getter, argv, minimum=0, maximum=100, suffix='') {
        const row = box(false, 'pou-row');
        const name = label(title, 'pou-key'), reading = label('', 'pou-value');
        name.x_expand = true;
        row.add_child(name); row.add_child(reading); this.body.add_child(row);
        const slider = new Slider.Slider(0);
        this.body.add_child(slider);
        let pending = null, sending = false, dragging = false, syncing = false, destroyed = false, lastWrite = 0;
        const send = async () => {
            if (sending) return;
            sending = true;
            try {
                while (pending !== null && this.alive) {
                    const value = pending;
                    pending = null;
                    await this.run([...argv, String(value) + suffix]);
                }
            } catch (e) {
                pending = null;
                if (this.alive) Main.notifyError('操作未完成', e.message.slice(0,400));
            } finally {
                sending = false;
                lastWrite = Date.now() / 1000;
                if (this.alive && !destroyed) this.refresh();
            }
        };
        slider.connect('destroy', () => { destroyed = true; });
        slider.connect('drag-begin', () => { dragging = true; });
        slider.connect('drag-end', () => { dragging = false; });
        slider.connect('notify::value', () => {
            if (syncing || this.updating) return;
            pending = Math.round(minimum + slider.value * (maximum - minimum));
            reading.text = pending + '%';
            send();
        });
        this.updaters.push(() => {
            if (sending || dragging || (this.data.timestamp ?? 0) < lastWrite) return;
            const value = getter();
            syncing = true;
            row.visible=slider.visible=!!fmt(value);
            slider.reactive = value != null;
            slider.value = ((value ?? minimum) - minimum) / (maximum - minimum);
            reading.text = fmt(value, '%');
            syncing = false;
        });
    }
    screenScale() {
        return Main.brightnessManager?.scales.find(scale =>
            scale.monitor.get_monitors().some(m => m.is_active() &&
                (m.is_laptop_panel?.() || /^(eDP|LVDS|DSI)/.test(m.get_connector?.() ?? ''))));
    }
    screenPercent() {
        const scale = this.screenScale();
        if (!scale) return null;
        if (Main.brightnessManager.autoBrightnessTarget >= 0) {
            const panel = scale.monitor.get_monitors().find(m => m.is_active() && m.get_backlight() &&
                (m.is_laptop_panel?.() || /^(eDP|LVDS|DSI)/.test(m.get_connector?.() ?? '')));
            const light = panel?.get_backlight();
            if (light && light.brightnessMax > light.brightnessMin)
                return Math.round((light.brightness - light.brightnessMin) * 100 /
                    (light.brightnessMax - light.brightnessMin));
        }
        return Math.round(100 * scale.value);
    }
    setScreenPercent(percent) {
        const scale = this.screenScale();
        if (!scale || !Number.isInteger(percent) || percent < 0 || percent > 100)
            throw new Error('内置屏幕亮度不可用');
        const switching = this.displaySettings.get_boolean('ambient-enabled') ||
            Main.brightnessManager.autoBrightnessTarget >= 0;
        this.manualTarget = percent;
        scale.value = percent / 100;
        if (this.displaySettings.get_boolean('ambient-enabled'))
            this.displaySettings.set_boolean('ambient-enabled', false);
        if (Main.brightnessManager.autoBrightnessTarget >= 0)
            Main.brightnessManager.autoBrightnessTarget = -1;
        if (switching && !this.transitionCheck) {
            this.transitionCheck = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 65, () => {
                this.sources.delete(this.transitionCheck);
                this.transitionCheck = 0;
                if (scale === this.screenScale() && !this.displaySettings.get_boolean('ambient-enabled') &&
                    Math.round(scale.value * 100) !== this.manualTarget)
                    scale.value = this.manualTarget / 100;
                return GLib.SOURCE_REMOVE;
            });
            this.sources.add(this.transitionCheck);
        }
    }
    brightnessSlider() {
        const slider = new Slider.Slider(0);
        const reading = label('', 'pou-value');
        this.body.add_child(reading);
        this.body.add_child(slider);
        let syncing = false, dragging = false;
        const sync = () => {
            const percent = this.screenPercent();
            syncing = true;
            slider.reactive = percent !== null;
            if (!dragging) slider.value = (percent ?? 0) / 100;
            reading.text = percent === null ? '' : `内置屏幕亮度　${percent}%`;
            reading.visible=slider.visible=percent !== null;
            syncing = false;
        };
        slider.connect('drag-begin', () => { dragging = true; });
        slider.connect('drag-end', () => {
            dragging = false;
            if (this.screenScale()) this.setScreenPercent(Math.round(slider.value * 100));
            sync();
        });
        slider.connect('notify::value', () => {
            if (!syncing && this.screenScale()) this.setScreenPercent(Math.round(slider.value * 100));
        });
        const manager = Main.brightnessManager;
        let bound = null;
        const bindScale = () => {
            bound?.disconnectObject(slider);
            bound = this.screenScale();
            bound?.connectObject('notify::value', sync, slider);
            bound?.connectObject('backlights-changed', sync, slider);
            sync();
        };
        manager?.connectObject('changed', bindScale, slider);
        bindScale();
    }
    page(route) {
        if (route === 'optical') route = 'power';
        this.route = ['home','battery','network','bluetooth','audio','power','touchpad','display'].includes(route)?route:'home';
        this.body.destroy_all_children();this.updaters=[];this.groups=[];
        const titles={home:'Oh My Panasonic',battery:'电源与电池',network:'Wi-Fi 与网络',bluetooth:'蓝牙设备',audio:'声音与设备',power:'电源策略',touchpad:'触控板',display:'显示与亮度'};
        const header=box(false,'pou-header'); if(this.route!=='home'){const back=this.actionButton('‹',()=>this.page('home'),header);back.x_expand=false;back.width=36;}header.add_child(label(titles[this.route],'pou-title'));this.body.add_child(header);
        if(this.route==='home') {
            const card=box(true,'pou-card');this.body.add_child(card);this.text(()=>fmt(this.value('capacity'),'%'),'pou-hero',card);this.text(()=>fmt(this.data.power_summary?.status),'pou-subtitle',card);this.meter(()=>this.value('capacity')==null?null:Number(this.value('capacity')),card);
            this.updaters.push(()=>{card.visible=card.get_children().some(c=>c.visible);});
            this.nav('电源与电池','battery-good-symbolic',()=>this.data.power_summary?.summary??'读取中','battery');
            this.nav('Wi-Fi','network-wireless-symbolic',()=>this.data.network?.connected?`${this.data.network.ssid}  ${signal(this.data.network.signal)} ${this.data.network.signal}%`:this.data.network?.enabled?'未连接':'已关闭','network');
            this.nav('蓝牙','bluetooth-active-symbolic',()=>this.data.bluetooth?.enabled?`已开启 · ${(this.data.bluetooth.devices??[]).filter(d=>d.connected).length} 台已连接`:'已关闭','bluetooth');
            this.nav('声音','audio-volume-high-symbolic',()=>{const d=this.data.audio?.sinks?.find(s=>s.default);return d?`${d.mute?'已静音':`${d.volume}%`} · ${d.name}`:'服务不可用';},'audio');
            this.nav('电源策略','power-profile-balanced-symbolic',()=>[fmt(modes[this.data.system?.profile]),fmt(fanPolicy(this.data))].filter(Boolean).join(' · '),'power');
            this.nav('触控板','input-touchpad-symbolic',()=>this.data.system?.pad_active?'圆形滚动已开启':'圆形滚动未开启','touchpad');
            this.nav('显示与亮度','display-brightness-symbolic',()=>fmt(this.screenPercent(),'%'),'display');
        } else if(this.route==='battery') {
            this.text(()=>fmt(this.value('capacity'),'%'),'pou-hero');
            this.stat('供电',()=>this.data.power_summary?.source??'');
            this.stat('充放电',()=>this.data.power_summary?.status??'');
            this.stat('充电功率',()=>this.data.power_summary?.charge_power??'');
            this.stat('放电功率',()=>this.data.power_summary?.discharge_power??'');
            this.stat('PD 协商电压',()=>this.data.battery?.power_input?.voltage_label??'');
            this.stat('实时输入功率',()=>['dc','typec','external'].includes(this.data.battery?.power_input?.source)?this.data.battery.power_input.input_label:'');
            this.stat('PD 合约',()=>this.data.battery?.power_input?.contract_details??'');
            this.stat('电池保养',()=>this.data.care?.label??'等待服务');
            this.stat('预计拔电',()=>this.data.care?.departure_prediction?.label??'');
            this.stat('充电上限',()=>this.data.care?.config?.limit == null ? '' : fmt(this.data.care.config.limit,'%'));
            this.buttons([['立刻充满',()=>this.control('battery','full')],
                ['',()=>this.control('battery',this.data.care?.config?.mode==='system'?'care':'system'),
                    ()=>this.data.care?.config?.mode==='system'?'使用软件电源管理':'恢复系统电池管理']]);
            this.text(()=>this.data.care?.error||'');
            this.chart('电量趋势 · 最近 24 小时 · 纵轴 0–100%',()=>this.data.battery_history??[],'%',100);
            this.text(()=>{const points=this.data.battery_history??[];return points.length>1?`${new Date(points[0][0]*1000).toLocaleTimeString()} → ${new Date(points.at(-1)[0]*1000).toLocaleTimeString()}`:'';});
            this.chart('电池充电功率 W · 最近 1 小时',()=>this.data.charge_power_history??[],'W');
            this.stat('满充容量',()=>fmt(this.metric('energy_full_wh'),' Wh'));
            this.stat('设计容量',()=>fmt(this.metric('energy_full_design_wh'),' Wh'));
            this.stat('电池电压',()=>fmt(this.metric('voltage_v'),' V'));
            this.stat('累计充电指数',()=>this.metric('cycle_count')==null?(this.metric('charge_index')??''):'');
            this.stat('电池循环次数',()=>this.metric('cycle_count')??'');
            this.stat('电池温度',()=>this.metric('temperature_c')==null?'':fmt(this.metric('temperature_c'),' °C'));
        } else if(this.route==='network') {
            this.text(()=>this.data.network?.ssid??'未连接','pou-title');
            this.actionButton('',()=>this.control('wifi',this.data.network?.enabled?'off':'on'),this.body,()=>this.data.network?.enabled?'关闭 Wi-Fi':'开启 Wi-Fi');
            this.stat('信号',()=>this.data.network?.connected&&Number.isFinite(this.data.network?.signal)?`${signal(this.data.network.signal)}  ${this.data.network.signal}%`:'');
            this.stat('频率',()=>fmt(this.data.network?.frequency,' MHz'));
            this.stat('链路速率',()=>fmt(this.data.network?.rate_mbps,' Mbps'));
            this.stat('IPv4',()=>this.data.network?.addresses?.join(', ')||'—');this.stat('网关',()=>this.data.network?.gateway??'—');
            this.stat('下载速率',()=>this.rxPoints.length?fmt(Math.round(this.rxPoints.at(-1)[1]),' KB/s'):'');
            this.stat('上传速率',()=>this.txPoints.length?fmt(Math.round(this.txPoints.at(-1)[1]),' KB/s'):'');
            this.stat('无线省电',()=>this.data.peripherals?.devices?.filter(d=>d.kind==='wifi').map(d=>d.value==='on'?'已开启':'低延迟').join('、')||'未提供');
            this.chart('最近 5 分钟流量 · 绿色下载 / 蓝色上传',()=>this.rxPoints,'KiB/s',null,()=>this.txPoints);
            this.text(()=> '附近网络 · 最近扫描结果');
            this.list(()=>this.data.network?.nearby??[],(n,p)=>{const r=box(false,'pou-row');const l=label(n.name);l.x_expand=true;r.add_child(l);r.add_child(label(`${n.secure?'▣ ':''}${signal(n.signal)} ${n.signal}%`,'pou-subtitle'));p.add_child(r);});
            this.buttons([['连接网络…',()=>{this.button.menu.close();this.launch(['/usr/bin/pou','gui','network']);}],['有线 / VPN',()=>this.settings('network')]]);
        } else if(this.route==='bluetooth') {
            this.actionButton('',()=>this.control('bluetooth',this.data.bluetooth?.enabled?'off':'on'),this.body,()=>this.data.bluetooth?.enabled?'关闭蓝牙':'开启蓝牙');
            this.stat('适配器',()=>this.data.bluetooth?.available?'已检测到':'不可用');
            this.list(()=>this.data.bluetooth?.devices??[],(d,p)=>{const c=box(true,'pou-card');c.add_child(label(d.name,'pou-value'));c.add_child(label(`${d.connected?'已连接':'已配对 · 未连接'}${d.battery!=null?` · 电量 ${d.battery}%`:''}`,'pou-subtitle'));if(d.battery!=null){const a=createPlot(100,true);a.points=[[0,d.battery]];c.add_child(a);}p.add_child(c);});
            this.actionButton('连接、配对与管理设备',()=>{this.button.menu.close();this.launch(['/usr/bin/pou','gui','bluetooth']);});
        } else if(this.route==='audio') {
            const output=()=>this.data.audio?.sinks?.find(s=>s.default);const input=()=>this.data.audio?.sources?.find(s=>s.default);
            this.text(()=>output()?.name??'没有输出设备','pou-value');this.slider('输出音量',()=>output()?.volume,['/usr/bin/pactl','set-sink-volume','@DEFAULT_SINK@'],0,100,'%');
            this.actionButton('',()=>this.action(['/usr/bin/pou','audio','mute','toggle']),this.body,()=>output()?.mute?'取消扬声器静音':'扬声器静音');
            this.list(()=>this.data.audio?.sinks??[],(d,p)=>this.actionButton(`${d.default?'✓ ':''}${d.name}`,()=>this.action(['/usr/bin/pou','audio','output',String(d.id)]),p));
            this.text(()=>input()?.name??'没有输入设备','pou-value');this.slider('麦克风音量',()=>input()?.volume,['/usr/bin/pactl','set-source-volume','@DEFAULT_SOURCE@'],0,100,'%');
            this.actionButton('',()=>this.action(['/usr/bin/pou','audio','mic-mute','toggle']),this.body,()=>input()?.mute?'取消麦克风静音':'麦克风静音');
            this.list(()=>this.data.audio?.sources??[],(d,p)=>this.actionButton(`${d.default?'✓ ':''}${d.name}`,()=>this.action(['/usr/bin/pou','audio','input',String(d.id)]),p));
        } else if(this.route==='power') {
            const sample=()=>this.data.power?.stale?null:this.data.power?.sample;
            const cards=box(false,'pou-actions');this.body.add_child(cards);
            for(const [name,getter] of [['平台功耗',()=>fmt(sample()?.platform_w,' W')],['CPU 封装',()=>fmt(sample()?.watts,' W')],['CPU 温度',()=>fmt(sample()?.temperature,' °C')]]) {
                const card=box(true,'pou-card');cards.add_child(card);this.text(()=>name,'pou-subtitle',card);this.text(getter,'pou-reading',card);
                this.updaters.push(()=>{card.visible=!!fmt(getter());});
            }
            this.updaters.push(()=>{cards.visible=cards.get_children().some(c=>c.visible);});
            this.stat('忙碌频率',()=>fmt(sample()?.busy_mhz,' MHz'));
            this.stat('含空闲平均频率',()=>fmt(sample()?.average_mhz,' MHz'));
            this.stat('CPU 活跃',()=>fmt(sample()?.busy_percent,'%'));
            this.stat('内存占用',()=>fmt(this.data.system?.memory_percent,'%'));
            this.chart('平台功耗 W · 最近 5 分钟',()=>this.data.power?.history?.filter(p=>Number.isFinite(p[4])).map(p=>[p[0],p[4]])??[],'W');
            this.chart('CPU 功率 W · 最近 5 分钟',()=>this.data.power?.history?.map(p=>[p[0],p[1]])??[],'W');
            this.chart('忙碌频率 MHz · 最近 5 分钟',()=>this.data.power?.history?.map(p=>[p[0],p[2]])??[],'MHz',3400);
            this.stat('风扇转速',()=>this.data.firmware?.fan_rpm==null?'未知':fmt(this.data.firmware.fan_rpm,' RPM'));
            this.stat('CPU 预设',()=>cpuModes[this.data.power?.mode]??'不可用');
            this.buttons(['eco','balanced','performance'].map(key=>[cpuModes[key],()=>this.control('cpu',key)]));
            this.buttons(['adaptive','boost','system'].map(key=>[cpuModes[key],()=>this.control('cpu',key)]));
            this.stat('长期功率上限',()=>fmt(this.data.power?.pl1_w,' W'));
            this.stat('短时功率上限',()=>fmt(this.data.power?.pl2_w,' W'));
            this.stat('EC 增强',()=>this.data.power?.ec_lease==null?'':this.data.power.ec_lease?'运行中':'未启用');
            this.stat('内核温度保护',()=>fmt(this.data.power?.kernel_guard_c,' °C'));
            this.text(()=>this.data.power?.error||'','pou-subtitle');
            this.stat('外设策略',()=>({follow:'随 CPU 预设',save:'省电',performance:'低延迟',system:'系统默认'}[this.data.peripherals?.requested]??''));
            this.text(()=>this.data.peripherals?.error??'');
            this.buttons([['随预设',()=>this.control('peripherals','follow')],['外设省电',()=>this.control('peripherals','save')],['低延迟',()=>this.control('peripherals','performance')],['恢复系统',()=>this.control('peripherals','system')]]);
            this.stat('系统电源模式',()=>modes[this.data.system?.profile]??'未知');this.buttons(Object.entries(modes).map(([key,value])=>[value,()=>this.control('profile',key)]));
            this.stat('风扇模式',()=>fanPolicy(this.data));this.stat('风扇反馈',()=>this.data.power?.fan?.feedback==null?'—':`${this.data.power.fan.feedback}%`);
            const fanButtons=[['标准',()=>this.control('fan','standard')],['静音',()=>this.control('fan','quiet')]];
            if(this.data.power?.fan?.available)fanButtons.push(['性能',()=>this.control('fan','performance')]);
            fanButtons.push(['恢复',()=>this.control('fan','restore')]);this.buttons(fanButtons);
            this.stat('固件动态功率限制',()=>({0:'关闭',1:'开启'}[this.data.firmware?.power_limits_enabled]??'未知'));
            const d=()=>this.data.optical??{};
            const ssdStart=this.body.get_n_children();
            this.text(()=>'SSD 电源','pou-title');
            const ssd=key=>{const d=this.data.ssd_power??{};return !d.available||d.stale?null:d[key];};
            this.stat('工作功率上限',()=>fmt(ssd('maximum_w'),' W'));
            this.stat('当前状态',()=>({0:'工作 PS0',1:'工作 PS1',2:'工作 PS2',3:'空闲 PS3',4:'深度空闲 PS4'}[ssd('current_state')]??'不可用'));
            this.stat('自动空闲节能',()=>ssd('apst_enabled')==null?'':ssd('apst_enabled')?'开启':'关闭');
            this.buttons([['随电源模式',()=>this.control('ssd-power','follow')],['系统管理',()=>this.control('ssd-power','system')]]);
            this.buttons([['省电',()=>this.control('ssd-power','save')],['均衡',()=>this.control('ssd-power','balanced')],['性能',()=>this.control('ssd-power','performance')]]);
            this.text(()=>this.data.ssd_power?.error??'');
            const ssdRows=this.body.get_children().slice(ssdStart);
            this.groups.push(ssdRows);
            this.updaters.push(()=>{if(!this.data.ssd_power?.available&&!this.data.ssd_power?.error)ssdRows.forEach(r=>r.hide());});
            const opticalStart=this.body.get_n_children();
            this.text(()=>'光驱电源','pou-title');
            const card=box(false,'pou-card');this.body.add_child(card);
            const emblem=box(true);emblem.y_align=Clutter.ActorAlign.CENTER;emblem.style='spacing: 10px; margin-right: 16px;';card.add_child(emblem);
            emblem.add_child(new St.Icon({icon_name:'media-optical-symbolic',icon_size:54}));
            this.text(()=>d().supported?(d().powered?'已供电':'已断电'):'不可用','pou-value',emblem);
            const details=box(true);details.x_expand=true;details.style='spacing: 8px;';card.add_child(details);
            this.text(()=>d().model??'','pou-value',details);
            this.text(()=>d().details?.capabilities??'','pou-subtitle',details);
            for(const [title,key] of [['光碟','media'],['光碟属性','attributes'],['光碟容量','capacity'],['已用空间','used'],['读写','access']]) this.stat(title,()=>d().details?.[key]??'',details);
            this.stat('默认策略',()=>d().policy==='on'?'保持供电':'空闲时关闭供电');
            this.stat('系统光驱设备',()=>d().block_present?'已识别':'未注册 / 已关闭');
            this.buttons([['保持供电',()=>this.control('optical','on')],['自动省电',()=>this.control('optical','auto')]]);
            this.text(()=>d().error||'');
            const opticalRows=this.body.get_children().slice(opticalStart);
            this.groups.push(opticalRows);
            this.updaters.push(()=>{if(!d().supported&&!d().error)opticalRows.forEach(r=>r.hide());});
        } else if(this.route==='touchpad') {
            this.stat('圆形滚动',()=>this.data.system?.pad_active?'正在运行':'未启用');
            this.buttons([['开启',()=>this.control('enable')],['恢复原生',()=>this.control('disable')]]);
            this.actionButton('灵敏度与滚动方向',()=>{this.button.menu.close();this.launch(['/usr/bin/pou','gui']);});
            this.actionButton('指针速度与轻触',()=>this.settings('mouse'));
        } else if(this.route==='display') {
            this.brightnessSlider();
            const automatic = this.actionButton('', () => {
                const enabled = this.displaySettings.get_boolean('ambient-enabled');
                this.displaySettings.set_boolean('ambient-enabled', !enabled);
            });
            const updateAutomatic = () => {
                automatic.label = '自动亮度　' + (this.displaySettings.get_boolean('ambient-enabled') ? '开' : '关');
            };
            this.displaySettings.connectObject('changed::ambient-enabled', updateAutomatic, automatic);
            updateAutomatic();
            this.actionButton('夜灯与显示设置',()=>{this.button.menu.close();this.launch(['/usr/bin/pou','gui','display']);});
        }
        this.errorLabel=label('','pou-error');this.errorLabel.clutter_text.line_wrap=true;this.errorLabel.hide();this.body.add_child(this.errorLabel);
        if (this.route === 'home') {
            const footer = box(false, 'pou-actions');
            this.body.add_child(footer);
            const system = SystemActions.getDefault();
            for (const [title, method, capability] of [
                ['挂起', 'activateSuspend', 'can-suspend'], ['关机', 'activatePowerOff', 'can-power-off'],
                ['重启', 'activateRestart', 'can-restart'],
            ]) {
                const button = this.actionButton(title, () => {
                    this.button.menu.close();
                    system[method]();
                }, footer);
                system.bind_property(capability, button, 'reactive', GObject.BindingFlags.SYNC_CREATE);
            }
            this.actionButton('进入面板', () => { this.button.menu.close(); this.launch(['/usr/bin/pou','gui']); }, footer);
        }
        this.update();
        if (this.button.menu.isOpen) this.refresh();
    }
    launch(argv) { try { Gio.Subprocess.new(argv,Gio.SubprocessFlags.NONE); } catch(e) {this.message(e.message);} }
    disableControls() {
        this.transitionCheck=0;
        this.alive=false;if(this.timer)GLib.Source.remove(this.timer);this.timer=0;
        this.stopTelemetry();
        this.compact?.destroy();this.compact=null;
        this.touchpadIndicator?.destroy();this.touchpadIndicator=null;
        this.batteryCancel?.cancel();this.batteryCancel=null;
        if(this.batteryChanged)this.batteryProxy?.disconnect(this.batteryChanged);
        if(this.batteryOwner)this.batteryProxy?.disconnect(this.batteryOwner);
        this.batteryChanged=this.batteryOwner=0;this.batteryProxy=null;
        for(const source of this.sources??[])GLib.Source.remove(source);this.sources?.clear();
        for(const process of this.processes??[])process.force_exit();this.processes?.clear();
        this.dbus?.unexport();if(this.owner)Gio.bus_unown_name(this.owner);this.owner=0;
        this.button?.destroy();this.button=null;this.updaters=[];
    }
}
