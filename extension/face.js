import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import Pango from 'gi://Pango';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as ModalDialog from 'resource:///org/gnome/shell/ui/modalDialog.js';

const directory = '/run/panasonic-on-ubuntu';
const uid = new Gio.Credentials().get_unix_user();
const socketPath = directory + '/consent-' + uid + '.sock';
const priority = GLib.PRIORITY_DEFAULT;

function observe(path, callback) {
    const file = Gio.File.new_for_path(path);
    const monitor = file.get_parent().monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, null);
    monitor.connect('changed', (_mon, from, to) => {
        if (from?.equal(file) || to?.equal(file)) callback();
    });
    return monitor;
}

export class FaceConsent {
    constructor() {
        this.monitor = observe(socketPath, () => this.connect());
        this.connect();
    }
    connect() {
        if (this.pending || this.stopped || !GLib.file_test(socketPath,GLib.FileTest.EXISTS)) return;
        const cancel = new Gio.Cancellable();
        this.pending = cancel;
        const socket = new Gio.SocketClient();
        socket.connect_async(Gio.UnixSocketAddress.new(socketPath), cancel, (client,res) => {
            try {
                const connection = client.connect_finish(res);
                if (this.pending !== cancel) { connection.close(null); return; }
                this.connection = connection;
                const input = new Gio.DataInputStream({base_stream:connection.get_input_stream()});
                input.read_line_async(priority,cancel,(stream,result) => {
                    try {
                        const [bytes] = stream.read_line_finish(result);
                        if (this.pending !== cancel) return;
                        const request = JSON.parse(new TextDecoder().decode(bytes));
                        this.present(request);
                    } catch (_) { if (this.pending === cancel) this.finish(); }
                });
            } catch (_) { if (this.pending === cancel) this.finish(); }
        });
    }
    present(request) {
        if (Main.sessionMode.isLocked || Main.sessionMode.isGreeter) { this.finish(); return; }
        this.timeout = GLib.timeout_add(priority,15500,() => { this.timeout=0; this.finish(); return GLib.SOURCE_REMOVE; });
        if (request.service === 'sudo' && typeof request.detail === 'string' && request.detail.startsWith('sudo ')) {
            const dialog = new ModalDialog.ModalDialog();
            this.modal = dialog;
            dialog.contentLayout.add_child(new St.Label({text:'本地终端 sudo 授权',style_class:'modal-dialog-headline'}));
            const command = new St.Label({text:request.detail});
            command.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
            command.clutter_text.line_wrap = true;
            command.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
            dialog.contentLayout.add_child(command);
            dialog.setButtons([{label:'使用密码',default:true,key:Clutter.KEY_Escape,action:()=>this.finish()},
                {label:'刷脸授权',action:()=>this.finish(true)}]);
            if (!dialog.open()) this.finish();
        } else if (request.service === 'polkit-1') {
            const until = GLib.get_monotonic_time() + 1500000;
            this.retry = GLib.timeout_add(priority,40,() => {
                const dialog = Main.layoutManager.modalDialogGroup.get_children().find(d =>
                    d.mapped && d._infoMessageLabel?.text === request.hint && d._user?.get_user_name() === GLib.get_user_name());
                if (!dialog && GLib.get_monotonic_time() < until) return GLib.SOURCE_CONTINUE;
                this.retry=0;
                if (!dialog || dialog.actionId === 'io.github.panasonic_on_ubuntu.face') { this.finish(); return GLib.SOURCE_REMOVE; }
                dialog._infoMessageLabel.text = '请选择本次授权方式';
                const row = new St.BoxLayout({style_class:'pou-actions',x_expand:true});
                this.row = row;
                row.connect('destroy',() => { this.row=null; if (this.pending) this.finish(); });
                const session = dialog._session;
                for (const [text, face] of [['使用密码',false],['刷脸授权',true]]) {
                    const button = new St.Button({label:text,style_class:'pou-button',can_focus:true,x_expand:true});
                    button.connect('clicked',() => this.finish(face && dialog.mapped && dialog._session === session));
                    row.add_child(button);
                }
                dialog.contentLayout.add_child(row);
                return GLib.SOURCE_REMOVE;
            });
        } else this.finish();
    }
    finish(face = false) {
        const connection = this.connection;
        this.connection = null;
        this.pending?.cancel();
        this.pending=null;
        if (this.retry) GLib.Source.remove(this.retry);
        if (this.timeout) GLib.Source.remove(this.timeout);
        this.retry=this.timeout=0;
        try {
            if (connection) connection.get_output_stream().write_all(new TextEncoder().encode(face?'face\n':'password\n'),null);
        } catch (_) { /* The requester closed its transaction. */ }
        try { connection?.close(null); } catch (_) { /* Already closed. */ }
        this.row?.destroy();
        this.modal?.close();
        this.modal=null;
    }
    destroy() { this.stopped=true; this.monitor.cancel(); this.finish(); }
}
