import St from 'gi://St';
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {parseState,pulseKind,position} from './touchpad-state.js';

const DIRECTORY='/run/pou-input';
const SOCKET=DIRECTORY+'/events.sock';

export class TouchpadIndicator {
    constructor(onVisibility=()=>{}) {
        this.onVisibility=onVisibility; this.alive=true; this.enabled=false;
        this.area=new St.DrawingArea({width:24,height:24,y_align:Clutter.ActorAlign.CENTER,visible:false});
        this.area.accessible_name='圆形触控板';
        this.area.connect('repaint',()=>this.paint());
        this.settings=new Gio.Settings({schema_id:'org.gnome.desktop.peripherals.touchpad'});
        this.state={points:[],scroll:0};this.lastPoints=[];this.seq=null;
    }
    setEnabled(enabled) {
        if (!this.alive || enabled===this.enabled) return;
        this.enabled=enabled;
        if (!enabled) { this.stop();return; }
        try {
            this.parentMonitor=Gio.File.new_for_path('/run').monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES,null);
            this.parentMonitor.connect('changed',(_,file,other)=>{
                if ([file,other].some(f=>f?.get_basename()==='pou-input')) this.watchDirectory();
            });
        } catch (_) { /* The existing directory can still be watched directly. */ }
        this.watchDirectory();
    }
    watchDirectory() {
        if (!this.enabled) return;
        this.monitor?.cancel();this.monitor=null;
        try {
            this.monitor=Gio.File.new_for_path(DIRECTORY).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES,null);
            this.monitor.connect('changed',(_,file,other)=>{
                if ([file,other].some(f=>f?.get_basename()==='events.sock')) this.connect();
            });
        } catch (_) { /* The service creates this directory when enabled. */ }
        this.connect();
    }
    connect() {
        if (!this.enabled || this.connection || this.connecting) return;
        this.cancel?.cancel();
        const cancel=this.cancel=new Gio.Cancellable();this.connecting=true;
        const client=new Gio.SocketClient();
        client.connect_async(Gio.UnixSocketAddress.new(SOCKET),cancel,(object,result)=>{
            let connection;
            try { connection=object.connect_finish(result); }
            catch (_) { if(cancel===this.cancel)this.connecting=false;return; }
            if (!this.enabled || cancel!==this.cancel || cancel.is_cancelled()) {connection.close(null);return;}
            this.connecting=false;this.connection=connection;
            this.reader=new Gio.DataInputStream({base_stream:connection.get_input_stream()});
            this.read(cancel);
        });
    }
    read(cancel) {
        this.reader.read_line_async(GLib.PRIORITY_DEFAULT,cancel,(stream,result)=>{
            if (cancel!==this.cancel || cancel.is_cancelled()) return;
            try {
                const [text]=stream.read_line_finish_utf8(result);
                if (text===null) throw new Error('closed');
                this.accept(parseState(text));this.read(cancel);
            } catch (_) {
                this.disconnect();
                if (this.enabled && !this.retry)
                    this.retry=GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT,1,()=>{this.retry=0;this.connect();return GLib.SOURCE_REMOVE;});
            }
        });
    }
    accept(state) {
        this.state=state;
        if (state.points.length) this.lastPoints=state.points;
        const pulse=pulseKind(state,this.settings.get_boolean('tap-to-click'),this.settings.get_string('click-method'));
        if (this.seq!==null && state.seq!==this.seq && pulse) {
            if (this.flashTimer) GLib.Source.remove(this.flashTimer);
            this.flash=pulse;this.phase=0;
            this.flashPoints=this.lastPoints.length===pulse ? this.lastPoints : pulse===2 ? [[35,50],[65,50]] : [[50,50]];
            this.flashTimer=GLib.timeout_add(GLib.PRIORITY_DEFAULT,85,()=>{
                if (++this.phase>=4) {this.flash=0;this.flashTimer=0;this.area.queue_repaint();return GLib.SOURCE_REMOVE;}
                this.area.queue_repaint();return GLib.SOURCE_CONTINUE;
            });
        }
        this.seq=state.seq;this.area.show();this.onVisibility();this.area.queue_repaint();
    }
    paint() {
        const cr=this.area.get_context();const [w,h]=this.area.get_surface_size();
        if (!w || !h) {cr.$dispose();return;}
        const color=this.area.get_theme_node().get_foreground_color();
        const rgb=[color.red/255,color.green/255,color.blue/255];
        const cx=w/2,cy=h/2,r=Math.min(w,h)*.41;
        cr.setSourceRGBA(...rgb,.08);cr.arc(cx,cy,r,0,Math.PI*2);cr.fill();
        cr.setLineWidth(1.5);
        if (this.state.scroll) {
            const dir=this.state.scroll,a=-Math.PI/2,b=a+dir*Math.PI*5/3;
            cr.setSourceRGBA(.3,.9,.65,1);
            if(dir>0)cr.arc(cx,cy,r,a,b);else cr.arcNegative(cx,cy,r,a,b);
            cr.stroke();
            const x=cx+r*Math.cos(b),y=cy+r*Math.sin(b),tx=-Math.sin(b)*dir,ty=Math.cos(b)*dir;
            cr.moveTo(x,y);cr.lineTo(x-4*tx+2*ty,y-4*ty-2*tx);cr.lineTo(x-4*tx-2*ty,y-4*ty+2*tx);cr.closePath();cr.fill();
        } else {cr.setSourceRGBA(...rgb,.8);cr.arc(cx,cy,r,0,Math.PI*2);cr.stroke();}
        const points=this.flash ? this.flashPoints : this.state.points;
        const alpha=this.flash && this.phase%2 ? .15 : 1;
        if (this.flash===2)cr.setSourceRGBA(.3,1,.45,alpha);else cr.setSourceRGBA(...rgb,alpha);
        for(const point of points) {const [x,y]=position(point,r);cr.arc(cx+x,cy+y,points.length>1?1.7:2,0,Math.PI*2);cr.fill();}
        cr.$dispose();
    }
    disconnect() {
        this.cancel?.cancel();this.cancel=null;this.connecting=false;
        try {this.connection?.close(null);} catch (_) { /* Already disconnected. */ }
        this.connection=null;this.reader=null;
        if(this.flashTimer)GLib.Source.remove(this.flashTimer);
        this.flashTimer=0;this.flash=0;this.seq=null;this.lastPoints=[];
        this.area.hide();this.onVisibility();
    }
    stop() {
        this.disconnect();this.monitor?.cancel();this.monitor=null;this.parentMonitor?.cancel();this.parentMonitor=null;
        if(this.retry)GLib.Source.remove(this.retry);this.retry=0;
    }
    destroy() {this.alive=false;this.enabled=false;this.stop();this.area.destroy();}
}
