#!/usr/bin/env python3
"""Explicit, bounded charge-current validation on the supported SV7.

Run in a transient systemd service; --run acquires a sleep inhibitor for this
test only. It uses normal idle consumption, with no CPU/disk stress workload.
The installed care service must be stopped before this tool will proceed.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

FW=Path('/sys/devices/platform/pou-battery')
SUPPLIES=Path('/sys/class/power_supply')
DMI=Path('/sys/class/dmi/id')
LOG=Path('/var/log/panasonic-on-ubuntu/battery-native-acceptance.jsonl')


def read(path): return path.read_text().strip()
def elapsed(): return time.clock_gettime(time.CLOCK_BOOTTIME)

def service_active():
    return subprocess.run(['systemctl','is-active','--quiet','pou-battery.service']).returncode==0


def snapshot(phase):
    fields=dict(line.split('=',1) for line in (SUPPLIES/'BAT1/uevent').read_text().splitlines())
    data={'time':time.time(),'phase':phase,'mode':int(read(FW/'mode')),
          'ac_override':int(read(FW/'ac_override')),'ac':int(read(SUPPLIES/'AC/online')),
          'capacity':int(fields['POWER_SUPPLY_CAPACITY']),'status':fields['POWER_SUPPLY_STATUS'],
          'watts':int(fields['POWER_SUPPLY_POWER_NOW'])/1e6,
          'energy_wh':int(fields['POWER_SUPPLY_ENERGY_NOW'])/1e6}
    if fields['POWER_SUPPLY_PRESENT']!='1' or not 40<=data['capacity']<=100 or not 0<=data['watts']<=150:
        raise RuntimeError('Battery sample invalid or below test floor')
    line=json.dumps(data)
    with LOG.open('a') as output: output.write(line+'\n'); output.flush()
    print(line,flush=True)
    return data


def set_mode(mode):
    if service_active():
        raise RuntimeError('The battery service resumed; ending direct validation')
    (FW/'mode').write_text(str(mode))
    actual=int(read(FW/'mode')); override=int(read(FW/'ac_override'))
    if actual!=mode or override!=(65 if mode==2 else 0):
        raise RuntimeError('Kernel/firmware readback mismatch')


def phase(name,mode,seconds):
    start=elapsed(); samples=[]
    while elapsed()-start<seconds:
        set_mode(mode); samples.append(snapshot(name)); time.sleep(5)
    return samples


def stopped(*unused): raise KeyboardInterrupt


def worker():
    for key,value in {'product_name':'CFSV7-3L','product_sku':'CF-SV7HD9VS','bios_version':'V3.11L22'}.items():
        if read(DMI/key)!=value: raise RuntimeError('Unsupported hardware/BIOS')
    if read(FW/'interface_version')!='1': raise RuntimeError('Unsupported battery module')
    if service_active():
        raise RuntimeError('Stop pou-battery.service before hardware acceptance')
    if read(FW/'mode')!='0' or read(FW/'lease_seconds')!='0':
        raise RuntimeError('Another battery owner is active; refusing to interfere')
    LOG.parent.mkdir(parents=True,exist_ok=True,mode=0o750)
    with LOG.open('a'): pass
    LOG.chmod(0o600)
    for signum in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP): signal.signal(signum,stopped)
    baseline=snapshot('baseline')
    if baseline['ac']!=1 or baseline['ac_override']!=0: raise RuntimeError('AC baseline missing')
    start=elapsed()
    try:
        # Below the usual recharge hysteresis is needed to observe real charge
        # current. A success acknowledgement at 100% cannot prove inhibition.
        while True:
            data=snapshot('idle_discharge')
            if data['capacity']<=93: break
            if elapsed()-start>5400: raise RuntimeError('Idle discharge exceeded 90 minutes')
            if data['mode']==2 and data['watts']>12:
                raise RuntimeError('System is not idle enough for gentle validation')
            set_mode(2); time.sleep(5)
        normal=phase('normal_before_hold',0,45)
        if not any(s['status']=='Charging' and s['watts']>1 for s in normal[-5:]):
            raise RuntimeError('No charging current observed; inhibit test is inconclusive')
        hold=phase('hold',1,90)
        if not all(s['ac']==1 and s['status'] in ('Not charging','Full') and s['watts']<0.5 for s in hold[-8:]):
            raise RuntimeError('Actual charge inhibition was not observed')
        normal=phase('normal_after_hold',0,45)
        if not any(s['status']=='Charging' and s['watts']>1 for s in normal[-5:]):
            raise RuntimeError('Charge current did not resume')
        snapshot('passed')
    finally:
        if service_active():
            snapshot('service-took-over')
        else:
            set_mode(0)
            time.sleep(3) # Allow ACPI power-supply notifications to catch up.
            snapshot('released')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',help='Run the real hardware test, temporarily preventing sleep')
    parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if not args.run: parser.print_help(); return
    if os.geteuid()!=0: raise RuntimeError('Run this hardware validation as root')
    if not args.worker:
        result=subprocess.run(['systemd-inhibit','--what=sleep','--mode=block',
            '--who=Oh My Panasonic validation','--why=Bounded idle battery-current validation',
            sys.executable,str(Path(__file__).resolve()),'--run','--worker'])
        raise SystemExit(result.returncode)
    with open('/run/lock/pou-battery-validation.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        worker()


if __name__=='__main__':
    from validation_cli import checked
    checked(main)
