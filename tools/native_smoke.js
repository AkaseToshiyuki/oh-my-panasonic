// Run only through gnome-shell-test-tool in its separate headless session.
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Scripting from 'resource:///org/gnome/shell/ui/scripting.js';
import Shell from 'gi://Shell';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

export const METRICS = {};
let execution;
export function run() {
    execution ??= inspect();
    return execution;
}
async function inspect() {
    print('POU_SMOKE_START');
    await Scripting.sleep(2000);
    const extension = Main.extensionManager.lookup('panasonic-on-ubuntu@local');
    if (!extension?.stateObj) throw new Error(`Extension not loaded: ${JSON.stringify(extension?.errors)}`);
    const app = extension.stateObj;
    print('POU_EXTENSION_LOADED');
    Main.overview.hide();
    for (let i = 0; i < 15 && !app.data.timestamp; i++) await Scripting.sleep(1000);
    if (!app.data.timestamp) throw new Error('No live telemetry');
    if (GLib.getenv('POU_SMOKE_RENDER_ONLY') !== '1' && (app.data.errors.network || app.data.errors.audio))
        throw new Error(JSON.stringify(app.data.errors));
    for (const page of ['home','battery','network','bluetooth','audio','power','optical','touchpad','display']) {
        print('POU_RENDER ' + page);
        app.page(page); app.button.menu.open(); await Scripting.sleep(700);
        if (!app.body.get_n_children()) throw new Error('Empty page: ' + page);
        const footerLabels = ['挂起', '关机', '重启', '进入面板'];
        const labels = [];
        function visit(actor) {
            if (typeof actor.label === 'string' && footerLabels.includes(actor.label))
                labels.push(actor.label);
            actor.get_children().forEach(visit);
        }
        visit(app.body);
        const expected = page === 'home' ? footerLabels : [];
        if (JSON.stringify(labels) !== JSON.stringify(expected))
            throw new Error('Unexpected footer on ' + page + ': ' + labels.join(', '));
        if (page === 'power') {
            const allLabels = [];
            function collect(actor) {
                if (typeof actor.text === 'string') allLabels.push(actor.text);
                actor.get_children().forEach(collect);
            }
            collect(app.body);
            for (const text of ['SSD 电源', '工作功率上限', '自动空闲节能'])
                if (!allLabels.includes(text)) throw new Error('Missing disk control: ' + text);
            if (allLabels.some(text => text.includes('MiB/s')))
                throw new Error('Retired bandwidth controls still visible');
            const adjustment = app.scroll.vadjustment;
            adjustment.value = adjustment.upper - adjustment.page_size;
            await Scripting.sleep(300);
        }
        const shot = new Shell.Screenshot();
        const destination = GLib.getenv('POU_SCREENSHOTS') ?? '/tmp';
        const stream = Gio.File.new_for_path(`${destination}/pou-native-${page}.png`).replace(null, false, Gio.FileCreateFlags.NONE, null);
        await new Promise((resolve, reject) => shot.screenshot(false, stream, (object, result) => {try { object.screenshot_finish(result);stream.close(null);resolve(); } catch(e) {reject(e);} }));
        print('POU_PAGE_OK ' + page);
    }
    app.hide(); if (app.button.visible) throw new Error('Hide failed');
    app.show(); if (!app.button.visible) throw new Error('Reopen failed');
    // Exercise the consent dialog without a socket or a real PAM transaction.
    const {FaceConsent} = await import(extension.dir.get_uri() + '/face.js');
    const consent = Object.create(FaceConsent.prototype);
    consent.present({service:'sudo',detail:'sudo true'});
    if (!consent.modal) throw new Error('Face consent dialog did not open');
    await Scripting.sleep(100);
    consent.finish(false);
    print('POU_FACE_CONSENT_UI_OK');
    print('POU_NATIVE_SMOKE_OK');
}
print('POU_AUTOMATION_IMPORTED');
// The distro perf-helper can wait for a portal unavailable in a headless bus.
// Drive this correctness check directly if the helper has not started it.
GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 5, () => {
    if (!execution) run().then(() => global.context.terminate()).catch(error => {
        logError(error, 'POU_NATIVE_SMOKE_FAILED');
        global.context.terminate();
    });
    return GLib.SOURCE_REMOVE;
});
