import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {selection, POLLED, cpuSample, cpuPercent, memoryPercent, powerValues, batteryValues, render} from './statusbar-format.js';

function read(file, cancel) {
    return new Promise((resolve,reject)=>file.load_contents_async(cancel,(object,result)=>{
        try { const [,bytes] = object.load_contents_finish(result); resolve(new TextDecoder().decode(bytes)); }
        catch(e) { reject(e); }
    }));
}

export class CompactStatus {
    constructor(label) {
        this.label = label; this.alive = true; this.visible = true;
        this.items = ['battery']; this.battery = {}; this.metrics = {};
        this.configCancel = new Gio.Cancellable();
        const directory = GLib.build_filenamev([GLib.get_user_config_dir(),'panasonic-on-ubuntu']);
        this.file = Gio.File.new_for_path(GLib.build_filenamev([directory,'statusbar.json']));
        try {
            GLib.mkdir_with_parents(directory,0o700);
            this.monitor = Gio.File.new_for_path(directory).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES,null);
            this.monitor.connect('changed',(_,file,other)=>{
                if (![file,other].some(f=>f?.get_basename()==='statusbar.json') || this.reloadTimer) return;
                this.reloadTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT,100,()=>{
                    this.reloadTimer=0; this.reload(); return GLib.SOURCE_REMOVE;
                });
            });
        } catch(e) { console.error(e); }
        this.reload();
    }
    async reload() {
        const generation = this.configGeneration = (this.configGeneration??0)+1;
        let items = ['battery'];
        try { items=selection(JSON.parse(await read(this.file,this.configCancel))?.items); } catch (_) { /* Missing settings use the default. */ }
        if (!this.alive || generation !== this.configGeneration) return;
        if (JSON.stringify(items) !== JSON.stringify(this.items)) { this.items=items; this.restart(); }
        else this.draw();
    }
    updateBattery(proxy) {
        this.battery = proxy?.g_name_owner ? batteryValues(name=>proxy.get_cached_property(name)?.unpack()) : {};
        this.draw();
    }
    setVisible(visible) { if (visible !== this.visible) { this.visible=visible; this.restart(); } }
    restart() {
        if (this.timer) GLib.Source.remove(this.timer);
        this.timer=0; this.cancel?.cancel(); this.cancel=null; this.previous=null; this.metrics={};
        this.draw();
        if (!this.alive || !this.visible || !this.items.some(k=>POLLED.includes(k))) return;
        const cancel = this.cancel = new Gio.Cancellable();
        this.sample(cancel);
        this.timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT,5,()=>{
            this.sample(cancel); return GLib.SOURCE_CONTINUE;
        });
    }
    async sample(cancel) {
        if (this.busy === cancel || cancel.is_cancelled()) return;
        this.busy=cancel;
        const values={};
        let previous=null;
        const load = path=>read(Gio.File.new_for_path(path),cancel).catch(()=>null);
        const [cpu,memory,power] = await Promise.all([
            this.items.includes('cpu') ? load('/proc/stat') : null,
            this.items.includes('memory') ? load('/proc/meminfo') : null,
            this.items.some(k=>['cpu_power','platform_power','temperature'].includes(k)) ? load('/run/panasonic-on-ubuntu/power.json') : null,
        ]);
        if (!this.alive || cancel !== this.cancel || cancel.is_cancelled()) return;
        if (cpu) { previous=cpuSample(cpu); values.cpu=cpuPercent(this.previous,previous); }
        if (memory) values.memory=memoryPercent(memory);
        if (power) { try { Object.assign(values,powerValues(JSON.parse(power),Date.now()/1000,GLib.get_monotonic_time()/1e6)); } catch (_) { /* Incomplete readings stay hidden. */ } }
        this.previous=previous; this.metrics=values; this.busy=null; this.draw();
    }
    draw() {
        if (!this.alive) return;
        const text=render(this.items,{...this.metrics,...this.battery});
        if (this.label.text !== text) this.label.text=text;
        this.label.visible=Boolean(text);
    }
    destroy() {
        this.alive=false;
        if (this.timer) GLib.Source.remove(this.timer);
        if (this.reloadTimer) GLib.Source.remove(this.reloadTimer);
        this.timer=this.reloadTimer=0;
        this.cancel?.cancel(); this.configCancel.cancel(); this.monitor?.cancel();
    }
}
