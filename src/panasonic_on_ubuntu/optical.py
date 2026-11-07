"""CF-SV7 optical power after complete optical-only AHCI driver teardown."""
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import time
from .power import RUNTIME, FIRMWARE, atomic, read

CONFIG = Path('/etc/panasonic-on-ubuntu/optical.json')
CONTROLLER = Path('/sys/bus/pci/devices/0000:00:17.0')
DRIVER = Path('/sys/bus/pci/drivers/ahci')
STATE = RUNTIME / 'optical.json'


def audit(event):
    directory=Path('/var/log/panasonic-on-ubuntu')
    directory.mkdir(mode=0o750,parents=True,exist_ok=True)
    handler=RotatingFileHandler(directory/'optical.log',maxBytes=256*1024,backupCount=1)
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    try:
        handler.emit(logging.LogRecord('optical',logging.INFO,__file__,0,event,(),None))
        handler.flush();os.fsync(handler.stream.fileno())
    finally:handler.close()


def supported():
    return (read(Path('/sys/class/dmi/id/product_name')) == 'CFSV7-3L'
            and read(Path('/sys/class/dmi/id/bios_version')) == 'V3.11L22'
            and read(FIRMWARE / 'optical_capabilities') == '9'
            and read(CONTROLLER / 'class') == '0x010601'
            and read(CONTROLLER / 'vendor') == '0x8086')


def blocks():
    return [p for p in Path('/sys/class/block').glob('*')
            if p.resolve().is_relative_to(CONTROLLER.resolve())]


def drives():
    return [p for p in blocks() if read(p / 'device/type') == '5']


def saved_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def status():
    devices, saved, capable = drives(), saved_state(), supported()
    try:
        policy = json.loads(CONFIG.read_text()).get('policy', 'auto')
    except (OSError, ValueError):
        policy = 'auto'
    return dict(available=bool(devices or saved), supported=capable, policy=policy,
                powered=read(FIRMWARE / 'optical_power') == '1' if capable else None,
                model=saved.get('model') or (read(devices[0]/'device/model') if devices else None),
                block_present=bool(devices), driver_bound=(CONTROLLER/'driver').exists(),
                error=saved.get('error', ''))


def restore():
    """D0 precedes binding: never probe an intentionally unpowered SATA port."""
    (FIRMWARE / 'optical_power').write_text('1\n')
    bound=CONTROLLER/'driver'
    if bound.exists() and bound.resolve()!=DRIVER.resolve():
        raise RuntimeError('光驱控制器由其他驱动管理，未修改绑定')
    if not bound.exists():
        (DRIVER/'bind').write_text(CONTROLLER.name+'\n')
    for _ in range(40):
        if drives():
            old=saved_state().get('runtime_control')
            if old in ('on','auto'):(CONTROLLER/'power/control').write_text(old+'\n')
            atomic(STATE, {'model':status()['model'],'error':''})
            return
        time.sleep(.25)
    raise RuntimeError('光驱供电已恢复，但设备未在 10 秒内重新出现')


def preflight():
    devices=blocks()
    if len(devices)!=1 or read(devices[0]/'device/type')!='5':
        raise RuntimeError('此控制器并非仅连接一个光驱，拒绝断电')
    if read(CONTROLLER/'remapped_nvme')!='0':
        raise RuntimeError('控制器可能包含重映射存储，拒绝断电')
    if (CONTROLLER/'driver').resolve()!=DRIVER.resolve():
        raise RuntimeError('没有检测到匹配的 AHCI 驱动')
    device=devices[0]
    dev_id=read(device/'dev')
    if any(len(parts:=line.split())>2 and parts[2]==dev_id for line in Path('/proc/self/mountinfo').read_text().splitlines()):
        raise RuntimeError('光盘已挂载，暂不关闭电源')
    fd=os.open('/dev/'+device.name,os.O_RDONLY|os.O_NONBLOCK|os.O_EXCL)
    try:
        if fcntl.ioctl(fd,0x5326,0)!=1:  # CDROM_DRIVE_STATUS == CDS_NO_DISC
            raise RuntimeError('有光盘、托盘打开或状态不明确，暂不关闭电源')
        if read(device/'device/device_busy')!='0':
            raise RuntimeError('光驱正在使用，暂不关闭电源')
    finally:
        os.close(fd)
    return read(device/'device/model')


def switch(value):
    if value not in ('on','off'):
        raise ValueError('Invalid optical state')
    if not supported():
        raise RuntimeError('未检测到已验证的光驱独立断电接口')
    RUNTIME.mkdir(mode=0o755,exist_ok=True)
    with open(RUNTIME/'optical.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if value=='on':
            audit('restoring optical power and driver')
            restore();audit('optical power restored');return
        if read(FIRMWARE/'optical_guard_version')!='1' and read(Path('/sys/module/pou_firmware/version'))!='0.3.0':
            raise RuntimeError('光驱断电需要带驱动解绑保护的固件后端（0.3.0 或更新版）')
        if read(FIRMWARE/'optical_power')=='0' and not (CONTROLLER/'driver').exists():
            return
        model=preflight()
        audit('preflight passed; detaching optical-only AHCI driver')
        old=read(CONTROLLER/'power/control')
        atomic(STATE,{'model':model,'error':'','transition':'off','runtime_control':old})
        try:
            (CONTROLLER/'power/control').write_text('on\n')
            # Driver removal synchronously destroys ATA/ZPODD resources. Do not
            # use SCSI delete followed immediately by a firmware power change.
            (DRIVER/'unbind').write_text(CONTROLLER.name+'\n')
            if (CONTROLLER/'driver').exists() or blocks():
                raise RuntimeError('AHCI 驱动资源未完全释放，未切断电源')
            (FIRMWARE/'optical_power').write_text('0\n')
            if read(FIRMWARE/'optical_power')!='0':
                raise RuntimeError('光驱固件断电读回失败')
            atomic(STATE,{'model':model,'error':'','runtime_control':old})
            audit('optical power off; driver detached; firmware readback=0')
        except Exception as exc:
            audit('transition failed; attempting restore: '+str(exc))
            try:
                restore()
            finally:
                atomic(STATE,{'model':model,'error':str(exc)})
            raise


def auto(attempts=1, delay=0):
    for attempt in range(attempts):
        if not supported() or status()['policy']!='auto':
            return
        try:
            switch('off')
            return
        except (OSError,RuntimeError) as exc:
            atomic(STATE,{'model':status()['model'],'error':str(exc)})
            if attempt+1<attempts:
                time.sleep(delay)


if __name__=='__main__':
    if len(sys.argv)==2 and sys.argv[1]=='auto':auto()
    elif len(sys.argv)==2 and sys.argv[1]=='resume':auto(attempts=5,delay=2)
    elif len(sys.argv)==2 and sys.argv[1]=='on':
        if supported():switch('on')
    else:raise SystemExit('Expected auto, resume or on')
