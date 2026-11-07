"""Battery snapshots and bounded local history; no firmware writes."""
import json
import os
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager
from .doctor import BATTERY_FIELDS, read_value, battery_metrics
from .power_input import detect as detect_power_input


def snapshot(root=Path('/sys/class/power_supply'), firmware=Path('/sys/devices/platform/pou-firmware')):
    batteries = []
    online = False
    for dev in sorted(root.glob('*')):
        kind = read_value(dev / 'type')['value']
        if kind == 'Battery':
            fields = {k: read_value(dev / k) for k in BATTERY_FIELDS}
            if fields['present']['value'] == '0':
                continue
            metrics = battery_metrics(fields)
            cycles = fields['cycle_count']['value']
            temperature = fields['temp']['value']
            if cycles and cycles.isdigit() and int(cycles) > 0:
                metrics['cycle_count'] = int(cycles)
            if temperature and temperature.lstrip('-').isdigit() and -500 <= int(temperature) <= 1000:
                metrics['temperature_c'] = int(temperature) / 10
            if dev.name == 'BAT1':
                index = read_value(firmware/'battery_charge_index')['value']
                raw = read_value(firmware/'battery_temp_decikelvin')['value']
                if index and index.isdigit() and int(index) < 65535:
                    metrics['charge_index'] = int(index)
                if raw and raw.isdigit() and 2231 <= int(raw) <= 3732:
                    metrics['temperature_c'] = (int(raw) * 10 - 27315) / 100
            batteries.append({'name': dev.name, 'fields': fields, 'metrics': metrics})
        elif read_value(dev / 'online')['value'] == '1':
            online = True
    return {'timestamp': int(time.time()), 'ac_online': online, 'batteries': batteries,
            'power_input': detect_power_input(root)}


@contextmanager
def database():
    path = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'panasonic-on-ubuntu'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path / 'battery.sqlite3', timeout=5)
    conn.execute('CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, data TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS notices (name TEXT PRIMARY KEY, ts INTEGER NOT NULL)')
    try:
        with conn:yield conn
    finally:
        conn.close()


def save_sample(data):
    with database() as conn:
        conn.execute('INSERT OR REPLACE INTO samples VALUES (?, ?)', (data['timestamp'], json.dumps(data)))
        conn.execute('DELETE FROM samples WHERE ts < ?', (data['timestamp'] - 30 * 86400,))


def history(hours=24):
    with database() as conn:
        return [json.loads(row[0]) for row in conn.execute('SELECT data FROM samples WHERE ts >= ? ORDER BY ts', (int(time.time()) - hours * 3600,))]


def monitor_once():
    data = snapshot()
    save_sample(data)
    from .care import status as care_status
    care = care_status()
    if care.get('supported'):
        return
    # UPower/GNOME owns critical battery actions; we only add a daily AC reminder.
    if data['ac_online'] and any(float(b['fields']['capacity']['value'] or 0) >= 95 for b in data['batteries']):
        with database() as conn:
            previous = conn.execute("SELECT ts FROM notices WHERE name='high-charge'").fetchone()
            if previous is None or data['timestamp'] - previous[0] >= 86400:
                import gi
                gi.require_version('Gio', '2.0')
                from gi.repository import Gio
                app = Gio.Application(application_id='io.github.panasonic_on_ubuntu', flags=Gio.ApplicationFlags.NON_UNIQUE)
                app.register(None)
                notice = Gio.Notification.new('Oh My Panasonic：电池已接近满电')
                notice.set_body('本机尚无可用的用户限充接口。长期插电时可按使用需要拔下电源；无需为保养刻意深度放电。')
                app.send_notification('high-charge', notice)
                conn.execute("INSERT OR REPLACE INTO notices VALUES ('high-charge', ?)", (data['timestamp'],))
