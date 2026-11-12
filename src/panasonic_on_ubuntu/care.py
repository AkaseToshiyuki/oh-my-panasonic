"""Battery care policy. Hardware writes use only the expiring kernel interface."""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import time
from .diagnostics import StateLog

CONFIG = Path('/etc/panasonic-on-ubuntu/battery-care.json')
STATE = Path('/var/lib/panasonic-on-ubuntu/battery-care.json')
RUNTIME = Path('/run/panasonic-on-ubuntu/battery-care.json')
FIRMWARE = Path('/sys/devices/platform/pou-battery')
SUPPLIES = Path('/sys/class/power_supply')
LOG_DIR = Path('/var/log/panasonic-on-ubuntu')
DEFAULT = {'mode': 'care', 'limit': 80, 'maintenance_days': 60, 'sleep_hold': True, 'poweroff_hold': True}
PHASES = {'system':'由系统管理充电', 'charging':'充电至保养上限', 'holding':'已暂停充电',
          'holding-pending':'正在暂停充电', 'recovery-failed':'自动恢复尚未确认',
          'battery':'使用电池', 'full':'本次充满', 'maintenance-full':'维护：充至满电',
          'maintenance-drain':'维护：缓慢放电', 'maintenance-pause':'维护：等待低负载',
          'unsupported':'电池控制不可用', 'error':'已恢复自动充电'}

class VerificationError(RuntimeError):
    pass

def read(path, default=None):
    try: return Path(path).read_text().strip()
    except OSError: return default

def load(path, default):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else dict(default)
    except (OSError, ValueError): return dict(default)

def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.new')
    with temp.open('w') as out:
        json.dump(data, out, ensure_ascii=False)
        out.flush(); os.fsync(out.fileno())
    temp.chmod(mode)
    temp.replace(path)

def config_valid(value):
    result = {**DEFAULT, **value}
    if result['mode'] not in ('system', 'care'): raise ValueError('Invalid battery policy')
    if type(result['limit']) is not int or not 50 <= result['limit'] <= 100: raise ValueError('Charge limit must be 50..100')
    if type(result['maintenance_days']) is not int or result['maintenance_days'] not in (0, 14, 30, 60, 90): raise ValueError('Invalid maintenance interval')
    if any(type(result[key]) is not bool for key in ('sleep_hold', 'poweroff_hold')):
        raise ValueError('Invalid offline charge setting')
    return result

def sample():
    fields = dict(line.split('=',1) for line in (SUPPLIES/'BAT1/uevent').read_text().splitlines())
    ports = [read(p/'online') for p in SUPPLIES.glob('ucsi-source-psy-*') if read(p/'scope') == 'System']
    usb_online = any(p in ('1','2') for p in ports) if ports and all(p in ('0','1','2') for p in ports) else None
    data = {'capacity':int(fields['POWER_SUPPLY_CAPACITY']), 'present':int(fields['POWER_SUPPLY_PRESENT']) == 1,
            'ac':read(SUPPLIES/'AC/online') == '1', 'status':fields['POWER_SUPPLY_STATUS'],
            'usb_online':usb_online,
            'watts':int(fields['POWER_SUPPLY_POWER_NOW']) / 1e6,
            'energy_wh':int(fields['POWER_SUPPLY_ENERGY_NOW']) / 1e6}
    if not data['present'] or not 0 <= data['capacity'] <= 100 or not 0 <= data['watts'] <= 150:
        raise ValueError('Invalid battery sample')
    return data

class Policy:
    """Pure state machine. Returns 0=auto, 1=hold, 2=forced battery."""
    def __init__(self, state=None):
        self.state = dict(state or {})
        self.phase = 'system'
        self.reason = ''

    def cancel(self):
        self.state.pop('override_until', None)
        self.state.pop('maintenance_phase', None)
        self.state.pop('maintenance_started', None)
        self.state.pop('maintenance_origin', None)
        self.state.pop('pause_until', None)
        self.state.pop('usb_drain', None)

    def request(self, config, now):
        request_id = config.get('request_id')
        if not request_id or request_id == self.state.get('request_id'): return
        action = config.get('action', 'configure')
        if action not in ('configure','limit','full','cancel-full','maintain','cancel-maintenance'):
            raise ValueError('Invalid battery request')
        self.state.pop('verification_error', None)
        if action == 'full':
            self.cancel(); self.state['override_until'] = now + 24*3600
        elif action == 'maintain':
            self.cancel(); self.state['maintenance_phase'] = 'full'; self.state['maintenance_started'] = now
            self.state['maintenance_origin'] = 'manual'
        elif action in ('limit','cancel-full','cancel-maintenance') or config['mode'] == 'system':
            if action == 'limit' and self.state.get('maintenance_phase'):
                self.state['last_maintenance'] = now
            self.cancel()
            if action == 'cancel-maintenance': self.state['last_maintenance'] = now
        self.state['request_id'] = request_id

    def tick(self, config, data, driver_mode, now):
        config = config_valid(config)
        self.request(config, now)
        if self.state.get('verification_error') and config['mode'] != 'system':
            raise ValueError(self.state['verification_error']+'；请重新应用设置后重试')
        self.reason = ''
        if self.state.get('limit') != config['limit']:
            self.state.pop('holding', None)
            self.state['limit'] = config['limit']
        if not data['present'] or not 0 <= data['capacity'] <= 100: raise ValueError('Battery unavailable')
        # Forced battery mode deliberately makes Linux AC/online report zero.
        usb_removed = self.state.get('usb_drain') and data.get('usb_online') is False
        connected = data['ac'] or data.get('usb_online') is True or (driver_mode == 2 and not usb_removed)
        if not connected:
            self.cancel(); self.state.pop('ac_since', None); self.state.pop('holding', None)
            self.phase = 'battery'; return 0
        if 'ac_since' not in self.state or now < self.state['ac_since']: self.state['ac_since'] = now
        if 'last_maintenance' not in self.state: self.state['last_maintenance'] = now
        near_full = ('满充后等待电量降至 94% 或以下再重新充电。'
                     if data['status']=='Not charging' and 95 <= data['capacity'] < 100 else '')
        if config['mode'] == 'system':
            self.reason = near_full
            self.cancel(); self.state.pop('holding', None); self.phase = 'system'; return 0
        if self.state.get('override_until', 0) > now:
            if data['capacity'] == 100 and data['status'] in ('Full','Not charging'):
                self.state.pop('override_until', None)
                self.state['last_maintenance'] = now
                self.state['holding'] = config['limit'] < 100
                self.phase = 'holding' if self.state['holding'] else 'charging'
                return int(self.state['holding'])
            self.reason = near_full
            self.phase = 'full'; return 0
        self.state.pop('override_until', None)
        interval = config['maintenance_days'] * 86400
        from .charge_habits import soon
        if (interval and config['limit'] < 100 and now-self.state['last_maintenance'] >= interval and
                now-self.state['ac_since'] >= 72*3600 and not self.state.get('maintenance_phase')
                and not soon(data.get('departure_prediction'),now)):
            self.state['maintenance_phase'] = 'full'; self.state['maintenance_started'] = now
            self.state['maintenance_origin'] = 'scheduled'
        maintenance = self.state.get('maintenance_phase')
        if maintenance and now-self.state.get('maintenance_started', now) > 24*3600:
            self.cancel(); self.state['last_maintenance'] = now
            self.reason = '维护超过 24 小时，已结束并恢复限充。'; maintenance = None
        if maintenance == 'full':
            if data['capacity'] >= 100 and data['status'] in ('Full','Not charging'):
                self.state['maintenance_phase'] = 'drain'; maintenance = 'drain'
            else:
                self.reason = near_full
                self.phase = 'maintenance-full'; return 0
        if maintenance == 'drain':
            if data['capacity'] <= config['limit']:
                self.cancel(); self.state['last_maintenance'] = now; self.state['holding'] = True
            else:
                # No synthetic workload: use normal idle consumption. Pause at
                # sustained high battery power instead of accelerating cycling.
                if driver_mode == 2 and data['watts'] > 12:
                    self.state['pause_until'] = now+120
                if self.state.get('pause_until', 0) > now:
                    self.phase = 'maintenance-pause'; return 1
                if driver_mode != 2:
                    self.state['usb_drain'] = data.get('usb_online') is True
                self.phase = 'maintenance-drain'; return 2
        limit = config['limit']
        if limit == 100:
            self.state.pop('holding', None); self.phase = 'charging'; return 0
        if data['capacity'] >= limit: self.state['holding'] = True
        elif data['capacity'] <= limit-3: self.state['holding'] = False
        self.phase = 'holding' if self.state.get('holding') else 'charging'
        if self.phase=='charging' and not self.reason: self.reason = near_full
        return 1 if self.state.get('holding') else 0

def status():
    data = load(RUNTIME, {})
    data['supported'] = read(FIRMWARE/'interface_version') == '1'
    data['offline_capabilities'] = int(read(FIRMWARE/'offline_capabilities') or 0)
    data['stale'] = not data.get('timestamp') or not 0 <= time.time()-data['timestamp'] < 15
    if data['stale']:
        data.update(phase='unsupported' if not data['supported'] else 'error', label='电池服务未运行', driver_mode=None)
    return data

def restore():
    if (FIRMWARE/'mode').exists(): (FIRMWARE/'mode').write_text('0')


def offline_requested(config, state, now, setting):
    return (config['mode'] == 'care' and config['limit'] < 100 and config.get(setting) is True
            and state.get('override_until', 0) <= now and state.get('maintenance_phase') != 'full'
            and not state.get('verification_error'))


def arm_offline(action):
    bit, setting = {'suspend': (1, 'sleep_hold'), 'poweroff': (2, 'poweroff_hold')}[action]
    if not int(read(FIRMWARE/'offline_capabilities') or 0) & bit:
        return
    if action == 'suspend':
        sleep_mode = read(Path('/sys/power/mem_sleep')) or ''
        if '[deep]' not in sleep_mode:
            from .hardware import selection
            if '[s2idle]' not in sleep_mode or selection()[0] != 'sr3':
                return
    config = config_valid(load(CONFIG, DEFAULT))
    state = load(STATE, {})
    enabled = offline_requested(config, state, time.time(), setting)
    (FIRMWARE/'arm_transition').write_text(str(bit) if enabled else '0')

def notify(text):
    address = os.environ.get('NOTIFY_SOCKET')
    if not address: return
    if address.startswith('@'): address = '\0'+address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.connect(address); client.sendall(text.encode())


class Wakeup:
    def __init__(self):
        self.path = RUNTIME.with_name('battery-wakeup.sock')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self.receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.receiver.bind(str(self.path))
        self.path.chmod(0o600)
        self.sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sender.setblocking(False)

    def set(self):
        try:
            self.sender.sendto(b'w', str(self.path))
        except BlockingIOError:
            pass

    def wait(self, seconds):
        self.receiver.settimeout(seconds)
        try:
            self.receiver.recv(8)
        except TimeoutError:
            pass

    def close(self):
        self.receiver.close()
        self.sender.close()
        self.path.unlink(missing_ok=True)

def record_maintenance(state, output):
    """Keep the first verified phases of a cycle beyond rotation of the sample log."""
    started = state.get('maintenance_started')
    cycle = state.get('maintenance_observation', {})
    if started is None and (not cycle or 'end' in cycle):
        return
    data = output['sample']
    entry = dict(at=output['timestamp'], phase=output['phase'], mode=output['driver_mode'],
                 capacity=data.get('capacity'), status=data.get('status'), watts=data.get('watts'),
                 ac=data.get('ac'), usb_online=data.get('usb_online'), error=output['error'])
    if started is None:
        cycle['end'] = entry
        return
    if cycle.get('started') != started:
        cycle = dict(started=started, origin=state.get('maintenance_origin', 'unknown'),
                     last_maintenance=state.get('last_maintenance'), ac_since=state.get('ac_since'),
                     limit=output['config']['limit'], interval_days=output['config']['maintenance_days'])
        state['maintenance_observation'] = cycle
    if entry['error']:
        cycle.setdefault('first_error', entry)
    elif entry['phase'] in ('maintenance-full', 'maintenance-drain', 'maintenance-pause'):
        cycle.setdefault(entry['phase'], entry)


def run():
    log = logging.getLogger('pou-battery-care'); log.setLevel(logging.INFO)
    LOG_DIR.mkdir(parents=True,exist_ok=True,mode=0o750)
    handler=RotatingFileHandler(LOG_DIR/'battery-care.log',maxBytes=1024*1024,backupCount=2)
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s')); log.addHandler(handler)
    policy=Policy(load(STATE, {}))
    from .care_session import Session
    session = Session(RUNTIME.with_name('ac-session.json'))
    from .charge_habits import Habits
    habits = Habits()
    stopping=False
    wake = Wakeup()
    def stop(*unused):
        nonlocal stopping
        stopping=True
        wake.set()
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, lambda *_: wake.set())
    previous_state=None; mismatch_since=None
    records = StateLog()
    restore()
    if (FIRMWARE/'arm_transition').exists():
        (FIRMWARE/'arm_transition').write_text('0')
    notify('READY=1')
    try:
        while not stopping:
            now=time.time(); error=''; data={}; desired=0; driver=0
            config=DEFAULT.copy()
            try:
                session.prepare(policy.state, now)
                config=config_valid(load(CONFIG,DEFAULT))
                # Consume explicit cancellation even if the optional module was
                # removed. A later reinstall must not replay an old cycle.
                policy.request(config,now)
                if read(FIRMWARE/'interface_version')!='1':
                    policy.phase='system' if config['mode']=='system' else 'unsupported'
                    policy.reason=''
                    if config['mode']!='system': error='本机没有已验证的电池控制模块'
                else:
                    data=sample(); driver=int(read(FIRMWARE/'mode','-1'))
                    try:
                        data['departure_prediction']=habits.observe(data,driver,now,config['mode']=='care')
                    except (OSError, ValueError, OverflowError) as learning_error:
                        # Optional history must never release the charge cap.
                        data['departure_prediction']=None
                        log.warning('departure learning unavailable: %s',learning_error)
                    desired=policy.tick(config,data,driver,now)
                    (FIRMWARE/'mode').write_text(str(desired))
                    driver=int((FIRMWARE/'mode').read_text())
                    actual=int((FIRMWARE/'ac_override').read_text())
                    if driver!=desired or (desired==2 and actual!=65) or (desired!=2 and actual!=0):
                        raise VerificationError('电池固件状态读回不匹配')
                    # Firmware acknowledgement is not an electrical measurement.
                    mismatch=(desired==1 and data['status']=='Charging' and data['watts']>0.5)
                    mismatch_since=(mismatch_since or now) if mismatch else None
                    if mismatch_since and now-mismatch_since>20:
                        raise VerificationError('暂停充电后仍检测到充电电流')
                    if mismatch and policy.phase=='holding': policy.phase='holding-pending'
            except Exception as exc:
                error=str(exc); policy.phase='error'
                if isinstance(exc,VerificationError): policy.state['verification_error']=error
                try: restore(); driver=0
                except OSError as recovery:
                    error += '; 恢复失败: '+str(recovery)
                    policy.phase='recovery-failed'
                    actual_mode=read(FIRMWARE/'mode')
                    driver=int(actual_mode) if actual_mode in ('0','1','2') else None
                log.warning('%s',error)
            label = PHASES.get(policy.phase, policy.phase)
            if policy.phase == 'charging' and data.get('status') != 'Charging':
                label = '等待充电'
            output={'timestamp':now,'config':{k:config[k] for k in DEFAULT},'phase':policy.phase,
                    'label':label,'error':error,'note':policy.reason,
                    'request_id':policy.state.get('request_id'),'driver_mode':driver,'sample':data,
                    'override_until':policy.state.get('override_until'),
                    'departure_prediction':data.get('departure_prediction'),
                    'last_maintenance':policy.state.get('last_maintenance'),
                    'next_maintenance':policy.state.get('last_maintenance',now)+config['maintenance_days']*86400 if config['maintenance_days'] else None,
                    'sleep_hold':bool(int(read(FIRMWARE/'offline_capabilities') or 0) & 1) and offline_requested(config,policy.state,now,'sleep_hold'),
                    'poweroff_hold':bool(int(read(FIRMWARE/'offline_capabilities') or 0) & 2) and offline_requested(config,policy.state,now,'poweroff_hold')}
            session.checkpoint(policy.state, now, bool(data) and not error)
            record_maintenance(policy.state, output)
            serialized=json.dumps(policy.state,sort_keys=True)
            if serialized!=previous_state:
                atomic(STATE,policy.state); previous_state=serialized
            atomic(RUNTIME,output,0o644)
            record_key = (output['phase'], error, output['request_id'], driver,
                          config['mode'], config['limit'], data.get('ac'), data.get('usb_online'))
            if records.due(record_key, time.monotonic()):
                log.info('%s',json.dumps(output,ensure_ascii=False))
            notify('WATCHDOG=1')
            if not stopping:
                wake.wait(5)
    finally:
        wake.close()
        try:
            restore(); log.info('service stopped; automatic charging restored')
        finally:
            log.removeHandler(handler); handler.close()

if __name__=='__main__':
    import sys
    if sys.argv[1:] == ['upgrade-defaults']:
        import fcntl
        with open('/run/lock/pou-battery-control.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            config = config_valid(load(CONFIG, DEFAULT))
            if config.get('care_defaults_version') != 2:
                if not config['maintenance_days']:
                    config['maintenance_days'] = DEFAULT['maintenance_days']
                config['care_defaults_version'] = 2
                atomic(CONFIG, config, 0o644)
    elif sys.argv[1:] == ['restore']:
        restore()
    elif sys.argv[1:] == ['service-stop']:
        restore()
        arm_offline('poweroff')
    elif sys.argv[1:] == ['arm-suspend']:
        arm_offline('suspend')
    else:
        run()
