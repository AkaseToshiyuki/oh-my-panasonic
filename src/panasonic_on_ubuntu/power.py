"""Root power policy and bounded live CPU history. No raw MSR writes."""
import errno
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import time
from . import power_profiles

CONFIG = Path('/etc/panasonic-on-ubuntu/power.json')
RUNTIME = Path('/run/panasonic-on-ubuntu')
FIRMWARE = Path('/sys/devices/platform/pou-firmware')
RAPL = Path('/sys/class/powercap/intel-rapl-mmio:0')
PSTATE = Path('/sys/devices/system/cpu/intel_pstate')
PRESETS = {
    'eco': dict(pl1=6, pl2=10, turbo=1, fan=1, profile='power-saver'),
    'balanced': dict(pl1=12, pl2=20, turbo=0, fan=0, profile='balanced'),
    'performance': dict(pl1=20, pl2=30, turbo=0, fan=0, profile='performance'),
    'adaptive': dict(pl1=20, pl2=30, turbo=0, fan=0, profile='performance'),
    'boost': dict(pl1=25, pl2=44, turbo=0, fan=0, profile='performance'),
}


def read(path, default=None):
    try:
        return path.read_text().strip()
    except OSError:
        return default


def atomic(path, value, mode=0o644):
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temp = path.with_suffix('.new')
    temp.write_text(json.dumps(value, ensure_ascii=False))
    temp.chmod(mode)
    temp.replace(path)


def status():
    try:
        data = json.loads((RUNTIME / 'power.json').read_text())
        sample=data.get('sample')
        data['stale'] = time.time()-data['timestamp']>12 or not sample or time.monotonic()-sample.get('received',0)>8
        return data
    except (OSError, ValueError, KeyError):
        return {'mode': 'system', 'stale': True, 'history': [], 'error': '监控服务尚未启动'}


def temperature():
    values = []
    for zone in Path('/sys/class/thermal').glob('thermal_zone*'):
        if read(zone / 'type') == 'x86_pkg_temp':
            try:
                values.append(int(read(zone / 'temp')) / 1000)
            except (TypeError, ValueError):
                pass
    return max(values) if values else None


def thermal_target(current, ceiling, temp):
    """90 C target, 94 C emergency reduction; retain silicon and thermald limits."""
    if temp is None or temp >= 94:
        return min(6,ceiling)
    if temp >= 90:
        return min(ceiling,max(min(6,ceiling),current-2))
    if temp <= 86:
        return min(ceiling, current + 1)
    return min(current,ceiling)


def valid_counters(values):
    limits={'PkgWatt':(0,200),'Bzy_MHz':(0,10000),'Avg_MHz':(0,10000),'Busy%':(0,100.1),'PkgTmp':(0,110)}
    return all(key in values and math.isfinite(values[key]) and low<=values[key]<=high for key,(low,high) in limits.items())


def counter_sample(header, row):
    fields = dict(zip(header, row))
    values = {k: float(fields[k]) for k in ('PkgWatt', 'Bzy_MHz', 'Avg_MHz', 'Busy%', 'PkgTmp')}
    if not valid_counters(values):
        return None
    try:
        platform = float(fields.get('SysWatt', 'nan'))
    except ValueError:
        platform = None
    return {'received': time.monotonic(), 'watts': values['PkgWatt'], 'busy_mhz': values['Bzy_MHz'],
            'average_mhz': values['Avg_MHz'], 'busy_percent': values['Busy%'], 'temperature': values['PkgTmp'],
            'platform_w': platform if platform is not None and math.isfinite(platform) and 0 < platform <= 1000 else None}


def permit_ec_lease(temp, fresh, active, cooling):
    return bool(fresh and temp is not None and not cooling and temp < (92 if active else 86))


def boost_ceiling(current, msr_limit):
    # Only the validated stock 20 W or higher policy is overridden. A lower
    # external limit remains authoritative; Intel's 25 W cTDP-up is the ceiling.
    return min(25, msr_limit, current if current < 20 else 25)


def notify(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect('\0' + address[1:] if address.startswith('@') else address)
            sock.sendall(message.encode())


class Controller:
    def __init__(self):
        self.baseline = None
        self.last = {}
        self.mode = 'system'
        self.ceiling = self.target = 0
        self.short_ceiling = None
        self.error = ''
        self.cool_until = 0
        self.guard_tripped = False

    def write(self, path, value):
        # Persist ownership before changing hardware, including a process kill
        # between the sysfs write and its readback.
        self.last[str(path)] = str(value)
        if self.baseline:
            atomic(RUNTIME / 'baseline.json', {**self.baseline, 'last': self.last}, 0o600)
        path.write_text(str(value) + '\n')
        actual = read(path)
        if actual != str(value):
            raise RuntimeError(f'{path.name}: 请求 {value}，读回 {actual}')

    def restore(self):
        # A partially restored policy must never renew EC leases on the next tick.
        self.mode = 'system'
        # Always release the EC lease, even if earlier writes only partly succeeded.
        if self.baseline:
            errors = []
            try:
                if read(FIRMWARE / 'performance_lease') == '1':
                    self.write(FIRMWARE / 'performance_lease', 0)
                elif read(FIRMWARE / 'power_limits_enabled') == '0':
                    self.write(FIRMWARE / 'power_limits_enabled', 1)
            except OSError as exc:
                errors.append(str(exc))
            for name, old in self.baseline['files'].items():
                try:
                    # Do not overwrite a stricter limit imposed externally.
                    current = read(Path(name))
                    if current == self.last.get(name):
                        Path(name).write_text(old + '\n')
                except OSError as exc:
                    errors.append(str(exc))
            profile_error = None
            try:
                current_profile = power_profiles.get_active()
                if current_profile == self.baseline.get('last_profile'):
                    power_profiles.set_active(self.baseline['profile'])
            except power_profiles.ProfileError as exc:
                profile_error = exc
            if errors:
                raise RuntimeError('; '.join(errors))
            if profile_error:
                raise profile_error
            (RUNTIME / 'baseline.json').unlink(missing_ok=True)
            self.baseline = None
            self.last = {}
        self.mode = 'system'

    def apply(self, mode, measured=None):
        self.restore()
        self.error = ''
        if mode == 'system':
            return
        if mode == 'thermal':
            raise RuntimeError('EC 解限温控档暂停：正在排查光驱切换与负载测试期间的系统无响应。')
        if mode not in PRESETS:
            raise ValueError('未知 CPU 预设')
        if read(Path('/sys/class/dmi/id/product_name')) != 'CFSV7-3L' or read(Path('/sys/class/dmi/id/bios_version')) != 'V3.11L22':
            raise ValueError('高级功耗预设尚未在此机型验证')
        temp = temperature()
        if read(RAPL / 'enabled') != '1' or temp is None:
            raise RuntimeError('缺少已启用的 MMIO RAPL 或 CPU 温度接口')
        if mode == 'boost' and read(FIRMWARE / 'performance_lease_version') != '1':
            raise RuntimeError('增强性能需要带独立温度保护的固件模块 0.4.0 或更新版')
        if mode == 'boost' and 'i5-8250U' not in Path('/proc/cpuinfo').read_text():
            raise RuntimeError('25 W 增强档目前只验证了本机的 i5-8250U')
        paths = [RAPL / f'constraint_{i}_power_limit_uw' for i in (0, 1)]
        paths += [PSTATE / 'no_turbo', FIRMWARE / 'fan_profile']
        files = {str(p): p.read_text().strip() for p in paths}
        profile = power_profiles.get_active()
        preset = PRESETS[mode]
        if any(int(files[str(p)]) <= 0 for p in paths[:2]):
            raise RuntimeError('RAPL 功率限制未启用或不可用，未接管 CPU')
        ceiling = int(files[str(paths[0])])/1e6
        short_limit = min(preset['pl2'], int(files[str(paths[1])])/1e6)
        if mode == 'boost':
            msr = int(read(Path('/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw'),'0'))/1e6
            if msr <= 0:
                raise RuntimeError('没有有效的处理器功率上限读数，未开启增强档')
            ceiling = min(boost_ceiling(ceiling,msr),int(files[str(paths[1])])/1e6)
            msr_short = int(read(Path('/sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw'),'0'))/1e6
            if msr_short <= 0:
                raise RuntimeError('短时功率上限不可用')
            short_limit = min(short_limit,msr_short)
            ceiling = min(ceiling,short_limit)
        self.ceiling = min(preset['pl1'], ceiling)
        self.target = min(12,self.ceiling) if mode in ('adaptive','boost') else self.ceiling
        self.short_ceiling = None
        if mode == 'boost':
            self.short_ceiling = short_limit
            fresh = measured and time.monotonic() - measured['received'] < 8
            if permit_ec_lease(temp, fresh, False, False):
                self.target = self.ceiling
            else:
                self.target = thermal_target(self.target, self.ceiling, temp if fresh else None)
            # Unlock the short burst only after the first guarded lease succeeds.
            short_limit = self.target
        self.cool_until = 0
        self.guard_tripped = False
        self.baseline = dict(files=files, profile=profile)
        atomic(RUNTIME / 'baseline.json', self.baseline, 0o600)
        try:
            self.baseline['last_profile'] = preset['profile']
            atomic(RUNTIME / 'baseline.json', {**self.baseline, 'last': self.last}, 0o600)
            power_profiles.set_active(preset['profile'])
            self.write(FIRMWARE / 'fan_profile', preset['fan'])
            self.write(PSTATE / 'no_turbo', preset['turbo'])
            self.write(paths[1], int(short_limit*1e6))
            self.write(paths[0], int(self.target*1e6))
            self.mode = mode
        except Exception:
            self.restore()
            raise

    def tick(self, measured):
        if self.mode == 'system':
            return
        temp = temperature()
        fresh = measured and time.monotonic() - measured['received'] < 8
        if self.mode in ('adaptive','boost'):
            self.target = thermal_target(self.target, self.ceiling, temp if fresh else None)
        elif temp is None or temp >= 94:
            self.target = min(6,self.ceiling)
            self.write(FIRMWARE / 'fan_profile', 0)
        path = RAPL / 'constraint_0_power_limit_uw'
        actual = int(read(path))
        if actual <= 0:
            raise RuntimeError('检测到外部关闭 RAPL 限制，已退出 CPU 接管')
        # Respect thermald/firmware reductions instead of racing them with repeated boosts.
        if actual < int(self.last.get(str(path), actual)):
            self.baseline['files'][str(path)] = str(actual)
            self.ceiling = min(self.ceiling, actual / 1e6)
            self.target = min(self.target, self.ceiling)
            self.error = '检测到外部温控降低功耗，已遵从；重新选择模式可重新评估。'
        desired = int(self.target*1e6)
        if actual != desired:
            self.write(path, desired)
        if self.mode == 'boost':
            active = read(FIRMWARE / 'performance_lease') == '1'
            reason = read(FIRMWARE / 'lease_reason')
            if reason in ('temperature','sensor') and not self.guard_tripped:
                self.guard_tripped = True
                self.cool_until = time.monotonic() + 60
                self.error = '内核已恢复固件限制，冷却至少 60 秒后重新评估。'
            if reason == 'active':
                self.guard_tripped = False
            allowed = self.target > 6 and permit_ec_lease(temp, fresh, active, time.monotonic() < self.cool_until)
            if allowed:
                # Renewal changes a deadline; the kernel independently reads DTS.
                try:
                    self.write(FIRMWARE / 'performance_lease', 1)
                except OSError as exc:
                    allowed = False
                    if exc.errno == errno.EBUSY:
                        # PM notifiers block renewal before userspace is frozen.
                        # Keep the selected policy; retry after fresh wake samples.
                        self.error = '等待系统睡眠切换完成，固件限制保持开启。'
                    elif exc.errno == errno.EAGAIN:
                        self.guard_tripped = True
                        self.cool_until = time.monotonic() + 60
                        self.error = '内核已恢复固件限制，冷却至少 60 秒后重新评估。'
                    else:
                        raise
                else:
                    if self.error.startswith(('内核已恢复','等待系统睡眠')):
                        self.error = ''
            elif active:
                self.write(FIRMWARE / 'performance_lease', 0)
            if self.short_ceiling is not None:
                short_path = RAPL / 'constraint_1_power_limit_uw'
                actual_short = int(read(short_path, '0'))
                if actual_short <= 0:
                    raise RuntimeError('短时功率限制已关闭')
                if actual_short < int(self.last.get(str(short_path), actual_short)):
                    self.short_ceiling = min(self.short_ceiling, actual_short / 1e6)
                    self.baseline['files'][str(short_path)] = str(actual_short)
                limit = self.short_ceiling if allowed else min(self.short_ceiling, self.target)
                if actual_short != int(limit * 1e6):
                    self.write(short_path, int(limit * 1e6))
        atomic(RUNTIME / 'baseline.json', {**self.baseline, 'last': self.last}, 0o600)


def recover(controller=None):
    from .ssd_power import restore as restore_ssd
    restore_ssd()
    from .peripherals import restore as restore_peripherals
    restore_peripherals()
    p = RUNTIME / 'baseline.json'
    if not p.exists():
        return
    value = json.loads(p.read_text())
    controller = controller or Controller()
    controller.baseline = value
    controller.last = value.get('last', {})
    # Older/incomplete snapshot: restore only the bounded known file whitelist.
    allowed = {str(RAPL / f'constraint_{i}_power_limit_uw') for i in (0, 1)}
    allowed |= {str(PSTATE / 'no_turbo'), str(FIRMWARE / 'fan_profile')}
    controller.baseline['files'] = {k: v for k, v in value['files'].items() if k in allowed}
    controller.restore()


def run():
    RUNTIME.mkdir(mode=0o755, exist_ok=True)
    log = logging.getLogger('pou-power')
    log.setLevel(logging.INFO)
    directory=Path('/var/log/panasonic-on-ubuntu')
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    handler=RotatingFileHandler(directory/'power.log',maxBytes=1024*1024,backupCount=2)
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    log.addHandler(handler)
    log.info('service start; pid=%s',os.getpid())
    controller = Controller()
    try:
        recover(controller)
    except power_profiles.ProfilesUnavailable as exc:
        log.warning('startup restore deferred: %s', exc)
    from .fan import Controller as FanController
    fan = FanController()
    def base_fan():
        if controller.mode in PRESETS:
            return PRESETS[controller.mode]['fan']
        try:
            return int(json.loads(Path('/etc/panasonic-on-ubuntu/fan.json').read_text())['profile'])
        except (OSError, ValueError, KeyError):
            return int(read(FIRMWARE / 'original_fan_profile', '0'))
    stopping = False
    from .peripherals import Controller as PeripheralController, restore as restore_peripherals
    peripherals=PeripheralController()
    from .ssd_power import Controller as SSDController, restore as restore_ssd
    ssd = SSDController()
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # turbostat uses Intel hardware counters and handles model-specific scaling.
    process = None
    selector = selectors.DefaultSelector()
    RUNTIME.mkdir(parents=True,exist_ok=True)
    wake_path = RUNTIME / 'power-wakeup.sock'
    wake_path.unlink(missing_ok=True)
    wake = socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
    wake.bind(str(wake_path))
    wake_path.chmod(0o600)
    selector.register(wake,selectors.EVENT_READ)
    measured, header, history, buffer = None, [], [], ''
    last_start=0
    def close_sampler():
        nonlocal process
        if process is None:return
        try:selector.unregister(process.stdout)
        except KeyError:pass
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=1)
        process.stdout.close()
        process=None
    def start_sampler(reason):
        nonlocal process,measured,header,buffer,last_start
        close_sampler()
        measured,header,buffer=None,[],''
        last_start=time.monotonic()
        log.info('starting hardware sampler: %s',reason)
        try:
            # A missing powercap psys directory does not mean the counter is
            # unavailable. Let turbostat probe its own perf/MSR sources.
            columns = 'Busy%,Bzy_MHz,Avg_MHz,PkgWatt,PkgTmp,SysWatt'
            process=subprocess.Popen(['/usr/bin/turbostat','--quiet','--Summary','--interval','2','--show',columns,'--out','/dev/stdout'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=0)
            selector.register(process.stdout,selectors.EVENT_READ)
        except OSError as exc:log.warning('sampler start failed: %s',exc)
    start_sampler('service start')
    from . import pd_request
    pd_stop = pd_request.start(atomic)
    previous_config = None
    retry_at = None
    request_id = None
    last_recorded = 0
    wall_offset=time.time()-time.monotonic()
    # Optical lifecycle is managed by its independent system service.
    notify('READY=1')
    try:
        while not stopping:
            new_offset=time.time()-time.monotonic()
            if abs(new_offset-wall_offset)>5:
                start_sampler('resume or clock change')
            wall_offset=new_offset
            sampling_age=time.monotonic()-(measured['received'] if measured else last_start)
            if time.monotonic()-last_start>5 and (process is None or process.poll() is not None or sampling_age>12):
                start_sampler('process exit or missing samples')
            config = read(CONFIG, '{}')
            changed = config != previous_config
            if changed or (retry_at is not None and time.monotonic() >= retry_at):
                previous_config = config
                retry_at = None
                try:
                    request = json.loads(config)
                    request_id = request.get('request_id')
                    controller.apply(request.get('mode', 'system'),measured)
                    log.info('mode=%s target=%s',controller.mode,controller.target)
                except power_profiles.ProfilesUnavailable as exc:
                    controller.error = str(exc)
                    retry_at = time.monotonic() + 5
                    log.warning('power service unavailable; retry in 5 s: %s', exc)
                except Exception as exc:
                    controller.error = str(exc)
                    log.warning('config rejected: %s',exc)
                handler.flush()
                os.fsync(handler.stream.fileno())
            for key, _ in selector.select(timeout=0 if changed else 2):
                if key.fileobj is wake:
                    wake.recv(64)
                    continue
                chunk = os.read(key.fileobj.fileno(), 16384).decode(errors='replace')
                if not chunk:
                    selector.unregister(key.fileobj)
                buffer += chunk
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    line = line.split()
                    if 'PkgWatt' in line:
                        header = line
                    elif len(line) == len(header) and header:
                        try:
                            new_sample = counter_sample(header, line)
                            if new_sample is None:
                                log.warning('invalid hardware-counter sample discarded')
                                continue
                            measured = new_sample
                            history.append([time.time(), measured['watts'], measured['busy_mhz'], measured['temperature'], measured['platform_w']])
                            history = history[-150:]
                        except (ValueError, KeyError):
                            pass
            try:
                controller.tick(measured)
            except Exception as exc:
                controller.error = str(exc)
                try:
                    controller.restore()
                except power_profiles.ProfilesUnavailable as restore_error:
                    retry_at = time.monotonic() + 5
                    log.warning('restore deferred: %s', restore_error)
            fresh = measured and time.monotonic() - measured['received'] < 8
            state = dict(timestamp=time.time(), mode=controller.mode, error=controller.error or ('' if fresh else '正在恢复硬件采样，暂不显示过期读数'), sample=measured if fresh else None, history=history, source='Linux turbostat · RAPL / APERF / MPERF', target_w=controller.target if controller.mode != 'system' else None, temperature_target=90 if controller.mode in ('adaptive','boost') else None)
            state['ec_lease'] = read(FIRMWARE / 'performance_lease') == '1'
            state['lease_reason'] = read(FIRMWARE / 'lease_reason')
            state['kernel_guard_c'] = int(read(Path('/sys/module/pou_firmware/parameters/lease_cutoff_c'),'0')) or None
            state['maximum_w'] = controller.ceiling if controller.mode != 'system' else None
            state['request_id'] = request_id
            state['pl1_w'] = int(read(RAPL / 'constraint_0_power_limit_uw', '0')) / 1e6 or None
            state['pl2_w'] = int(read(RAPL / 'constraint_1_power_limit_uw', '0')) / 1e6 or None
            state['no_turbo'] = read(PSTATE / 'no_turbo')
            try:
                state['fan'] = fan.tick(base_fan())
            except (OSError, ValueError, TypeError) as exc:
                try:
                    fan.release()
                except OSError:
                    pass  # Kernel releases an unrenewed request independently.
                from .fan import CONFIG as fan_config
                try:
                    fan_id = json.loads(fan_config.read_text()).get('request_id')
                except (OSError, ValueError):
                    fan_id = None
                state['fan'] = {'error': str(exc), 'available': False, 'request_id': fan_id}
            atomic(RUNTIME / 'power.json', state)
            try:
                peripherals.tick(controller.mode)
            except Exception as exc:
                log.warning('peripheral policy: %s',exc)
            try:
                ssd.tick(controller.mode)
            except (OSError, ValueError, RuntimeError) as exc:
                log.warning('SSD power policy: %s', exc)
            if time.monotonic()-last_recorded>=10:
                last_recorded=time.monotonic()
                log.info('%s',json.dumps({k:v for k,v in state.items() if k!='history'},ensure_ascii=False))
                handler.flush()
                os.fsync(handler.stream.fileno())
            notify('WATCHDOG=1')
    finally:
        if pd_stop is not None:
            pd_stop.set()
        close_sampler()
        selector.close()
        wake.close()
        wake_path.unlink(missing_ok=True)
        try:
            fan.restore(base_fan())
        except OSError as exc:
            log.warning('fan restore: %s', exc)
        try:
            controller.restore()
        except power_profiles.ProfileError as exc:
            log.warning('power profile restore deferred; snapshot retained: %s', exc)
        restore_ssd()
        restore_peripherals()
        log.info('service stopped; pending profile restore=%s', controller.baseline is not None)


if __name__ == '__main__':
    try:
        if len(sys.argv) > 1 and sys.argv[1] == 'restore':
            recover()
        else:
            run()
    except power_profiles.ProfileError as exc:
        # Expected service shutdown/unavailability is not an Apport exception.
        try:
            print(str(exc), file=sys.stderr)
        except BrokenPipeError:
            pass
        raise SystemExit(1)
