"""Read-only live desktop metrics. Network names stay in the local session."""
import json
import os
from pathlib import Path
import subprocess
import time
import shutil
import gi

gi.require_version('NM', '1.0')
from gi.repository import NM, Gio, GLib
from .battery import snapshot, history
from .firmware import status as firmware_status
from .desktop import backlight
from .power import status as power_status
from .optical import status as optical_status
from .peripherals import status as peripheral_status
from .power_profiles import current_or_none


def command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=3, env={**os.environ, 'LC_ALL': 'C'})
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def network():
    client = NM.Client.new(None)
    result = {'enabled': client.wireless_get_enabled(), 'hardware': client.wireless_hardware_get_enabled(), 'available': False, 'connected': False, 'nearby': []}
    devices = [d for d in client.get_devices() if isinstance(d, NM.DeviceWifi)]
    if not devices:
        return result
    device = next((d for d in devices if d.get_active_access_point()), devices[0])
    interface = device.get_iface()
    result.update(available=True, interface=interface)
    ap = device.get_active_access_point()
    def name(point):
        ssid = point.get_ssid()
        return NM.utils_ssid_to_utf8(ssid.get_data()) if ssid else '隐藏网络'
    if ap:
        result.update(connected=True, ssid=name(ap), signal=ap.get_strength(), frequency=ap.get_frequency(), rate_mbps=round(device.get_bitrate()/1000, 1), secure=bool(ap.get_flags() or ap.get_wpa_flags() or ap.get_rsn_flags()))
    ip = device.get_ip4_config()
    result['addresses'] = [a.get_address() for a in ip.get_addresses()] if ip else []
    result['gateway'] = ip.get_gateway() if ip else None
    points = sorted(device.get_access_points(), key=lambda a: a.get_strength(), reverse=True)
    seen = set()
    for point in points:
        ssid = name(point)
        if ssid in seen:
            continue
        seen.add(ssid)
        result['nearby'].append({'name': ssid, 'signal': point.get_strength(), 'secure': bool(point.get_flags() or point.get_wpa_flags() or point.get_rsn_flags())})
        if len(result['nearby']) == 7:
            break
    for key in ('rx_bytes', 'tx_bytes'):
        try:
            result[key] = int((Path('/sys/class/net') / interface / 'statistics' / key).read_text())
        except (OSError, ValueError):
            result[key] = None
    return result


def bluetooth():
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    objects = bus.call_sync('org.bluez', '/', 'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects', None, None, Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
    adapters = [v['org.bluez.Adapter1'] for v in objects.values() if 'org.bluez.Adapter1' in v]
    result = {'available': bool(adapters), 'enabled': any(a.get('Powered') for a in adapters), 'devices': []}
    for interfaces in objects.values():
        device = interfaces.get('org.bluez.Device1')
        if not device or not (device.get('Paired') or device.get('Connected')):
            continue
        result['devices'].append({'name': device.get('Alias', device.get('Name', '蓝牙设备')), 'connected': bool(device.get('Connected')), 'paired': bool(device.get('Paired')), 'icon': device.get('Icon'), 'battery': interfaces.get('org.bluez.Battery1', {}).get('Percentage')})
    result['devices'].sort(key=lambda d: (not d['connected'], d['name']))
    return result


def audio():
    info = json.loads(command(['pactl', '-f', 'json', 'info']))
    result = {'available': True}
    for kind, default in [('sinks', info['default_sink_name']), ('sources', info['default_source_name'])]:
        devices = []
        for item in json.loads(command(['pactl', '-f', 'json', 'list', kind])):
            if kind == 'sources' and item['name'].endswith('.monitor'):
                continue
            values = [v['value'] / 65536 * 100 for v in item.get('volume', {}).values()]
            devices.append({'id': item['index'], 'name': item.get('description', item['name']), 'default': item['name'] == default, 'mute': item['mute'], 'volume': round(sum(values)/len(values)) if values else None, 'state': item.get('state', 'UNKNOWN')})
        result[kind] = devices
    return result


def system():
    cpu = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    mem = {parts[0].rstrip(':'): int(parts[1]) for line in Path('/proc/meminfo').read_text().splitlines() if len(parts := line.split()) >= 2}
    temperatures = []
    for zone in Path('/sys/class/thermal').glob('thermal_zone*'):
        try:
            if (zone / 'type').read_text().strip() in ('x86_pkg_temp', 'acpitz'):
                temperatures.append(int((zone / 'temp').read_text()) / 1000)
        except (OSError, ValueError):
            pass
    return {'cpu_total': sum(cpu), 'cpu_idle': cpu[3] + cpu[4], 'memory_total_gb': round(mem['MemTotal']/1048576, 1), 'memory_percent': round((1-mem['MemAvailable']/mem['MemTotal'])*100), 'temperature': max(temperatures) if temperatures else None, 'profile': current_or_none(), 'pad_active': command(['systemctl', 'show', 'pou-wheelpad.service', '-p', 'ActiveState', '--value']) == 'active'}


def collect():
    data = {'timestamp': time.time(), 'battery': snapshot(), 'firmware': firmware_status(), 'power': power_status(), 'optical': optical_status(), 'peripherals':peripheral_status(), 'errors': {}}
    from .optical_info import collect as optical_details
    try:data['optical']['details']=optical_details(data['optical'])
    except (OSError,ValueError,TypeError):data['optical']['details']={}
    from .ssd_power import status as ssd_status
    data['ssd_power'] = ssd_status()
    from .care import status as care_status
    data['care'] = care_status()
    from .power_summary import describe
    samples = history()
    data['power_summary'] = describe(data['battery'], data['care'], samples)
    from .face import status as face_status
    data['face'] = face_status()
    disk=shutil.disk_usage('/')
    data['hardware']={'model':Path('/sys/class/dmi/id/product_name').read_text().strip(),'kernel':os.uname().release,'root_total_gb':round(disk.total/1e9,1),'root_used_gb':round(disk.used/1e9,1)}
    for name, function in [('network', network), ('bluetooth', bluetooth), ('audio', audio), ('system', system)]:
        try:
            data[name] = function()
        except Exception as exc:
            data[name] = {}
            data['errors'][name] = str(exc)[:200]
    try:
        _, monitor = backlight()
        data['brightness'] = round((monitor['value']-monitor['min'])*100/(monitor['max']-monitor['min']))
    except Exception:
        data['brightness'] = None
    points = []
    charge_points = []
    for sample in samples:
        if sample['batteries']:
            b = sample['batteries'][0]
            value = b['fields']['capacity']['value']
            if value is not None:
                points.append([sample['timestamp'], float(value)])
            if data['timestamp']-3600 <= sample['timestamp'] <= data['timestamp']:
                watts = b.get('metrics',{}).get('battery_power_w')
                status = b['fields'].get('status',{}).get('value')
                if watts is not None and status in ('Charging','Full','Discharging','Not charging'):
                    charge_points.append([sample['timestamp'], watts if status=='Charging' else 0])
    stride = max(1, (len(points)+179)//180)
    data['battery_history'] = points[::stride]
    if points and data['battery_history'][-1] != points[-1]:
        data['battery_history'].append(points[-1])
    data['charge_power_history'] = charge_points
    return data
