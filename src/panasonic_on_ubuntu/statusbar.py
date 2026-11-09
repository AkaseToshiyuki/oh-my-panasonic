"""Per-user choices for the compact panel readout."""
import json
import math
import os
from pathlib import Path
import tempfile

OPTIONS = (
    ('battery', '电池电量'), ('cpu', 'CPU 占用率'),
    ('remaining', '预计可用时间'), ('battery_power', '电池充放电功率'),
    ('cpu_power', 'CPU 功耗'), ('platform_power', '平台功耗'),
    ('temperature', 'CPU 温度'), ('memory', '内存占用率'),
)
KEYS = {key for key, _ in OPTIONS}
DEFAULT = ['battery']
MAX_ITEMS = 5


def path():
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config')))/'panasonic-on-ubuntu/statusbar.json'


def normalize(value):
    if not isinstance(value, list):
        return DEFAULT.copy()
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item in KEYS))[:MAX_ITEMS]


def load():
    try:
        value = json.loads(path().read_text(encoding='utf-8'))
        return normalize(value.get('items')) if isinstance(value, dict) else DEFAULT.copy()
    except (OSError, ValueError):
        return DEFAULT.copy()


def save(items):
    if not isinstance(items, list) or normalize(items) != items:
        raise ValueError('最多选择五项状态信息')
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix='.statusbar-', delete=False) as output:
            name = output.name
            json.dump({'items':items}, output, ensure_ascii=False)
        os.replace(name, target)
    finally:
        if name:
            Path(name).unlink(missing_ok=True)


class FallbackReadout:
    """Reuse the compatibility tray's existing refresh cycle."""
    def __init__(self):
        self.previous = None

    def collect(self, snapshot):
        from .power import status
        from .power_summary import number
        items = load();values = {};charging = False
        if 'cpu' in items:
            try:
                with Path('/proc/stat').open() as stream:
                    counters = [int(n) for n in stream.readline().split()[1:9]]
                current = (sum(counters), counters[3]+counters[4])
                if self.previous:
                    total,idle = (a-b for a,b in zip(current,self.previous))
                    if total>0 and 0<=idle<=total:values['cpu']=100*(1-idle/total)
                self.previous=current
            except (OSError,ValueError,IndexError):self.previous=None
        else:self.previous=None
        if 'memory' in items:
            try:
                info={line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
                if info['MemTotal']>0:values['memory']=100*(1-info['MemAvailable']/info['MemTotal'])
            except (OSError,ValueError,KeyError):pass
        if any(k in items for k in ('cpu_power','platform_power','temperature')):
            power=status()
            if not power.get('stale'):
                sample=power.get('sample') or {}
                values.update(cpu_power=sample.get('watts'),platform_power=sample.get('platform_w'),temperature=sample.get('temperature'))
        battery=(snapshot.get('batteries') or [{}])[0];fields=battery.get('fields',{})
        values['battery']=number(fields,'capacity')
        state=fields.get('status',{}).get('value');charging=state=='Charging'
        watts=battery.get('metrics',{}).get('battery_power_w')
        if state in ('Charging','Discharging'):values['battery_power']=abs(watts) if watts is not None else None
        if state=='Discharging':
            seconds=number(fields,'time_to_empty_now')
            if not seconds or seconds<0:
                for unit,rate in (('energy','power'),('charge','current')):
                    amount=number(fields,unit+'_now');speed=number(fields,rate+'_now')
                    if amount and speed:
                        seconds=3600*amount/abs(speed);break
            values['remaining']=seconds
        return render(items,values,charging)


def render(items, values, charging=False):
    result=[]
    for key in normalize(items):
        n=values.get(key)
        if not isinstance(n,(int,float)) or not math.isfinite(n) or n<0:continue
        if key in ('battery','cpu','memory'):
            if n<=100:result.append(f"{dict(battery='',cpu='CPU ',memory='内存 ')[key]}{math.floor(n+.5)}%")
        elif key=='remaining':
            if 0<n<7*86400:
                minutes=max(1,math.ceil(n/60));hours,minutes=divmod(minutes,60)
                result.append('剩余 '+(f'{hours}h' if hours else '')+(f'{minutes}m' if minutes else ''))
        elif key=='temperature':
            if n<=150:result.append(f'{math.floor(n+.5)}°C')
        elif n<=1000:
            prefix=dict(cpu_power='CPU',platform_power='平台',battery_power='充电' if charging else '放电')[key]
            result.append(f'{prefix} {n:.1f}W')
    return ' · '.join(result)
