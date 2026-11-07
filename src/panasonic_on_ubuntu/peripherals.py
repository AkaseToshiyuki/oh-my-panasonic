"""Reversible wireless sleep and NVMe APST latency policies through Linux APIs."""
import json
from pathlib import Path
import re
import subprocess
import time

ROOT=Path('/run/panasonic-on-ubuntu')
CONFIG=Path('/etc/panasonic-on-ubuntu/peripherals.json')
BASELINE=ROOT/'peripherals-baseline.json'


def read(path):
    try:return path.read_text().strip()
    except OSError:return None


def save(path,value,mode=0o644):
    path.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
    temporary=path.with_suffix('.new')
    temporary.write_text(json.dumps(value,ensure_ascii=False))
    temporary.chmod(mode);temporary.replace(path)


def wifi_state(interface):
    result=subprocess.run(['/usr/sbin/iw','dev',interface,'get','power_save'],capture_output=True,text=True,timeout=3)
    if result.returncode:return None
    return next((s for s in ('on','off') if result.stdout.strip().endswith(': '+s)),None)


def devices():
    found={}
    for device in Path('/sys/class/net').glob('*'):
        if (device/'wireless').exists():
            try:state=wifi_state(device.name)
            except (OSError,subprocess.TimeoutExpired):state=None
            if state is not None:found['wifi:'+device.name]=dict(kind='wifi',name=device.name,value=state)
    for device in Path('/sys/class/nvme').glob('nvme[0-9]*'):
        if not re.fullmatch(r'nvme\d+',device.name):continue
        path=device/'power/pm_qos_latency_tolerance_us'
        if path.exists():
            found['nvme:'+device.name]=dict(kind='nvme',name=device.name,value=read(path),model=read(device/'model'))
    return found


def write(item,value):
    if item['kind']=='wifi':
        if value not in ('on','off') or not re.fullmatch(r'[\w.-]{1,15}',item['name']):raise ValueError('Invalid wireless target')
        subprocess.run(['/usr/sbin/iw','dev',item['name'],'set','power_save',value],check=True,capture_output=True,timeout=3)
        actual=wifi_state(item['name'])
    elif item['kind']=='nvme':
        if not re.fullmatch(r'nvme\d+',item['name']) or not (str(value).isdigit() or value in ('auto','any')):raise ValueError('Invalid NVMe target')
        path=Path('/sys/class/nvme')/item['name']/'power/pm_qos_latency_tolerance_us'
        path.write_text(str(value)+'\n');actual=read(path)
    else:raise ValueError('Unknown peripheral')
    if actual!=str(value):raise RuntimeError(f"{item['name']}: 设置读回不匹配")


def restore():
    if not BASELINE.exists():return
    baseline=json.loads(BASELINE.read_text())
    current=devices()
    remaining={}
    for key,item in baseline.items():
        if key not in current:continue
        if current[key]['value']!=item.get('last'):continue
        try:write(current[key],item['original'])
        except (OSError,RuntimeError,ValueError,subprocess.SubprocessError):remaining[key]=item
    if remaining:
        save(BASELINE,remaining,0o600)
        raise RuntimeError('部分外设策略尚未恢复，将在服务重启时重试')
    BASELINE.unlink(missing_ok=True)


def desired_policy(requested,cpu_mode):
    if requested=='follow':return {'eco':'save','performance':'performance','adaptive':'performance','boost':'performance'}.get(cpu_mode,'system')
    if requested not in ('system','save','performance'):raise ValueError('Invalid peripheral policy')
    return requested


def status():
    try:
        result=json.loads((ROOT/'peripherals.json').read_text())
        result['stale']=time.time()-result['timestamp']>45
        return result
    except (OSError,ValueError):return {'stale':True,'mode':'system','devices':[],'error':'等待系统服务'}


class Controller:
    def __init__(self):
        self.previous=None
        self.previous_request=None
        self.next_refresh=0
        self.error=''
        self.baseline={}

    def tick(self,cpu_mode):
        requested=json.loads(read(CONFIG) or '{}').get('mode','follow')
        mode=desired_policy(requested,cpu_mode)
        changed=mode!=self.previous or requested!=self.previous_request
        if not changed and time.monotonic()<self.next_refresh:return
        self.next_refresh=time.monotonic()+20
        if changed:
            restore();self.baseline={};self.error='';self.previous=mode;self.previous_request=requested
        found=devices()
        if mode!='system':
            errors=[]
            for key,item in found.items():
                target=('on' if mode=='save' else 'off') if item['kind']=='wifi' else ('100000' if mode=='save' else '20000')
                if item['value']==target:continue
                baseline=self.baseline.setdefault(key,{'original':item['value'],'last':target})
                baseline['last']=target
                save(BASELINE,self.baseline,0o600)
                try:write(item,target)
                except (OSError,RuntimeError,ValueError,subprocess.SubprocessError) as exc:errors.append(f"{item['name']}: {exc}")
            self.error='; '.join(errors)
            found=devices()
        save(ROOT/'peripherals.json',dict(timestamp=time.time(),requested=requested,mode=mode,devices=list(found.values()),error=self.error))
