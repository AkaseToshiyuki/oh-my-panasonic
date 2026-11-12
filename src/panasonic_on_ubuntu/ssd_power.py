"""Volatile, model-scoped NVMe operational power states; never sets idle states."""
import ctypes
import fcntl
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import stat
import struct
import time

ROOT = Path('/run/panasonic-on-ubuntu')
CONFIG = Path('/etc/panasonic-on-ubuntu/ssd-power.json')
BASELINE = ROOT/'ssd-power-baseline.json'
MODES = ('follow', 'system', 'save', 'balanced', 'performance')
VERIFIED = {
    ('ZHITAI TiPlus5000 1TB', 'ZTA10666'): (6.5, 5.8, 3.6),
    ('SilverMake 2268XT2 B58R 1TB', 'X0430M00'): (4.5, 2.4, .6),
}
ADMIN = struct.Struct('<BBHIIIQQ10I')
ADMIN_IOCTL = 0xc0484e41


def load(path, default):
    try: return json.loads(path.read_text())
    except FileNotFoundError: return default


def save(path, value, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.new')
    temporary.write_text(json.dumps(value))
    temporary.chmod(mode)
    temporary.replace(path)


def system_namespace():
    number = os.stat('/').st_dev
    node = (Path('/sys/dev/block')/f'{os.major(number)}:{os.minor(number)}').resolve()
    if (node/'partition').exists(): node = node.parent
    match = re.fullmatch(r'(nvme\d+)n\d+', node.name)
    if not match: raise ValueError('系统盘不是可识别的 NVMe 设备')
    return '/dev/' + node.name


def system_controller():
    return re.match(r'(/dev/nvme\d+)n', system_namespace())[1]


def identify(data):
    if len(data) != 4096: raise ValueError('NVMe 设备信息长度不符')
    model = data[24:64].decode('ascii', errors='replace').strip()
    firmware = data[64:72].decode('ascii', errors='replace').strip()
    expected = VERIFIED.get((model, firmware))
    if expected is None: raise ValueError('此 SSD 固件尚未验证电源控制')
    states = []
    for index in range(min(32, data[263]+1)):
        offset = 2048+32*index
        maximum, flags = struct.unpack_from('<H', data, offset)[0], data[offset+3]
        if not flags & 2:
            states.append({'state': index, 'maximum_w': maximum * (.0001 if flags & 1 else .01)})
    if [s['state'] for s in states] != [0,1,2] or any(abs(s['maximum_w']-w) > 1e-6 for s,w in zip(states,expected)):
        raise ValueError('SSD 电源档位与已验证型号不符')
    return dict(model=model, firmware=firmware, states=states,
                fingerprint=hashlib.sha256(data[4:72]).hexdigest())


class Device:
    def __init__(self, path=None):
        self.path = path or system_controller()
        if not re.fullmatch(r'/dev/nvme\d+', self.path): raise ValueError('Invalid NVMe path')

    def _command(self, opcode, field, value=0, length=0):
        allowed = ((opcode,field,length) in ((6,1,4096),(10,2,0),(10,12,256)) and value == 0
                   or (opcode,field,length) == (9,2,0) and 0 <= value <= 255 and value & 31 <= 2)
        if not allowed: raise ValueError('Unsupported NVMe operation')
        data = ctypes.create_string_buffer(length) if length else None
        command = bytearray(ADMIN.pack(opcode,0,0,0,0,0,0,ctypes.addressof(data) if data else 0,
                                      0,length,field,value,0,0,0,0,1500,0))
        descriptor = os.open(self.path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            if not stat.S_ISCHR(os.fstat(descriptor).st_mode): raise ValueError('Not an NVMe controller')
            result = fcntl.ioctl(descriptor, ADMIN_IOCTL, command, True)
            if result: raise RuntimeError(f'NVMe 命令未成功：{result:#x}')
        finally: os.close(descriptor)
        return ADMIN.unpack(command)[-1], data.raw if data else b''

    def active_value(self):
        # Admin requests do not necessarily leave APST idle states. A single
        # read-only namespace block reveals the last operational state.
        if self.path != system_controller(): raise ValueError('SSD 不是当前系统盘')
        descriptor = os.open(system_namespace(), os.O_RDONLY | os.O_DIRECT | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            if not stat.S_ISBLK(os.fstat(descriptor).st_mode): raise ValueError('Not a block device')
            with mmap.mmap(-1,4096) as buffer:
                if os.preadv(descriptor,[buffer],0) != 4096: raise OSError('Short SSD state probe')
                return self._command(10,2)[0]
        finally: os.close(descriptor)

    def read(self, wake=False):
        information = identify(self._command(6,1,length=4096)[1])
        value = self.active_value() if wake else self._command(10,2)[0]
        apst, table = self._command(10,12,length=256)
        if value > 255 or value & 31 not in ((0,1,2) if wake else (0,1,2,3,4)):
            raise RuntimeError('SSD 当前工作档位不可识别')
        information.update(device=self.path, value=value, selected=value & 31,
                           apst_enabled=bool(apst & 1), apst_hash=hashlib.sha256(struct.pack('<I',apst)+table).hexdigest())
        return information

    def write(self, value, fingerprint):
        before = self.read(wake=True)
        if before['fingerprint'] != fingerprint: raise RuntimeError('SSD 已更换，停止应用旧设置')
        self._command(9,2,value=value)  # SV=0: never writes the persistent default.
        after = self.read(wake=True)
        if after['value'] != value or before['apst_hash'] != after['apst_hash']:
            self._command(9,2,value=before['value'])
            raise RuntimeError('SSD 档位或自动空闲策略读回不一致，已请求恢复')
        return after


def desired(mode, cpu):
    if mode not in MODES: raise ValueError('SSD 电源策略无效')
    if mode == 'follow': mode = {'eco':'save','balanced':'balanced','performance':'performance',
                                 'adaptive':'performance','boost':'performance'}.get(cpu,'system')
    return {'system':None,'save':2,'balanced':1,'performance':0}[mode]


def release(device=None):
    saved = load(BASELINE, None)
    if not saved: return
    device = device or Device(saved['device'])
    current = device.read(wake=True)
    if current['fingerprint'] != saved['fingerprint']: raise RuntimeError('SSD 已更换，原设置待核对')
    if current['value'] == saved['last']:
        device.write(saved['original'], saved['fingerprint'])
    BASELINE.unlink(missing_ok=True)


def restore():
    try: release()
    except (OSError, ValueError, RuntimeError) as error:
        save(ROOT/'ssd-power.json', dict(timestamp=time.time(),available=False,error=str(error)))


def status():
    try:
        result = load(ROOT/'ssd-power.json', {})
        result['stale'] = not 0 <= time.time()-result.get('timestamp',0) < 40
        return result
    except (OSError, ValueError): return {'available':False, 'stale':True}


class Controller:
    def __init__(self):
        self.previous = None
        self.next_check = 0
        self.retry = False
        self.external = False

    def tick(self, cpu):
        config = load(CONFIG, {'mode':'follow'})
        mode = config.get('mode','follow')
        target = desired(mode, cpu)
        key = (mode,target,config.get('request_id'))
        changed = key != self.previous
        if not changed and time.monotonic() < self.next_check: return
        self.next_check = time.monotonic()+15
        output = dict(timestamp=time.time(),requested=mode,request_id=config.get('request_id'),available=False,error='')
        try:
            device = Device()
            if changed or self.retry:
                release(device)
                self.external = False
            information = device.read(wake=target is not None and (changed or self.retry))
            saved = load(BASELINE, None)
            if saved and (saved['fingerprint'] != information['fingerprint'] or
                          information['selected'] <= 2 and saved['last'] != information['value']):
                release(device)
                self.external = True
            if target is not None and not self.external and (changed or self.retry):
                # A stricter pre-existing hardware cap remains authoritative.
                selected = max(target, information['selected'])
                value = (information['value'] & ~31) | selected
                save(BASELINE, dict(device=device.path, fingerprint=information['fingerprint'],
                                    original=information['value'], last=value), 0o600)
                information = device.write(value, information['fingerprint'])
            saved = load(BASELINE, None)
            selected = information['selected'] if information['selected'] <= 2 else (saved['last'] & 31 if saved else None)
            output.update(available=True, model=information['model'], selected=selected, current_state=information['selected'],
                          maximum_w=information['states'][selected]['maximum_w'] if selected is not None else None,
                          apst_enabled=information['apst_enabled'])
            if self.external: output['error'] = 'SSD 电源状态已由其他程序更改'
            self.retry = False
        except (OSError, ValueError, RuntimeError) as error:
            output['error'] = str(error)
            self.retry = True
        self.previous = key
        save(ROOT/'ssd-power.json', output)
