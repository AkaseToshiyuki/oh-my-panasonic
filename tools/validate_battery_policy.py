#!/usr/bin/env python3
"""Exercise the installed care service on a connected CF-SV7 with USB PD.

Start below the firmware recharge threshold with measured charging current.
Uses the regular control helper and restores the initial system policy. An
intervening user configuration takes precedence. Run in a bounded transient
unit with a sleep inhibitor so the logger and service can observe the cycle.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from panasonic_on_ubuntu import care
from panasonic_on_ubuntu.power_input import detect


def run(upper_only=False):
    if os.geteuid() or care.read(Path('/sys/class/dmi/id/product_sku')) != 'CF-SV7HD9VS':
        raise RuntimeError('Requires root on the supported target')
    original = care.load(care.CONFIG, {})
    if care.config_valid(original)['mode'] != 'system' or care.read(care.FIRMWARE/'mode') != '0':
        raise RuntimeError('Start in system-managed charging mode')
    initial = care.sample()
    highest_start = 98 if upper_only else 94
    if initial['usb_online'] is not True or not initial['ac'] or not 50 <= initial['capacity'] <= highest_start:
        raise RuntimeError(f'A charging USB PD baseline at or below {highest_start}% is required')
    if initial['status'] != 'Charging' or initial['watts'] <= 1:
        raise RuntimeError('No live charging current at start')
    target = initial['capacity'] + 1 if upper_only else min(initial['capacity'] + 1, 94)
    original_exists = care.CONFIG.exists()
    expected_id = original.get('request_id')
    log = care.LOG_DIR / time.strftime('battery-policy-acceptance-%Y%m%d-%H%M%S.jsonl')
    print(log, flush=True)
    def check_owner():
        if care.load(care.CONFIG, {}).get('request_id') != expected_id:
            raise RuntimeError('Configuration changed externally; leaving the user setting in place')
    def act(*args):
        nonlocal expected_id
        check_owner()
        completed = subprocess.run(['/usr/libexec/pou-control', *args], timeout=50)
        record = care.load(care.CONFIG, {})
        if args[0]=='battery-limit' and (record.get('action')!='configure' or record.get('limit')!=int(args[1])):
            raise RuntimeError('Charge limit changed externally')
        if args[0]=='battery' and record.get('action')!=args[1]:
            raise RuntimeError('Battery action changed externally')
        expected_id = record.get('request_id')
        completed.check_returncode()
    def wait(phase, condition, minutes, consecutive=2):
        deadline = time.monotonic() + 60 * minutes
        count = 0
        while time.monotonic() < deadline:
            check_owner()
            state = care.status()
            sample = care.sample()
            port = detect()['ports']
            row = {'time':time.time(), 'step':phase, 'phase':state.get('phase'),
                   'mode':state.get('driver_mode'), **sample,
                   'pd_w':port[0]['contract_w'] if len(port)==1 else None}
            with log.open('a') as output:
                print(json.dumps(row), file=output)
            if state.get('error') or state.get('stale') or sample['usb_online'] is not True:
                raise RuntimeError(f'Service error or power disconnected: {row}')
            count = count + 1 if condition(row) else 0
            if count >= consecutive:
                print(json.dumps(row), flush=True)
                return
            time.sleep(5)
        raise TimeoutError(phase)
    def holding(row):
        return (row['phase']=='holding' and row['mode']==1 and row['ac'] and
                row['status'] in ('Full','Not charging') and row['watts']<0.5)
    def interrupted(*unused):
        raise InterruptedError('Validation interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        act('battery-limit', str(target))
        if upper_only:
            wait('below-cap', lambda r: r['phase']=='charging' and r['capacity']<target and
                 r['status']=='Charging' and r['watts']>1, 2, 1)
        wait('cap', lambda r: holding(r) and r['capacity']==target, 35, 4)
        subprocess.run(['systemctl','restart','pou-battery.service'],check=True,timeout=25)
        wait('restart-hold', holding, 2)
        if upper_only:
            # Exercise fresh kernel initialization and persisted user settings
            # without forcing a reboot of the user's graphical session.
            subprocess.run(['systemctl','stop','pou-battery.service'],check=True,timeout=25)
            try:
                if care.read(care.FIRMWARE/'mode')!='0' or care.read(care.FIRMWARE/'lease_seconds')!='0':
                    raise RuntimeError('Service stop did not release the battery lease')
                subprocess.run(['modprobe','-r','pou_battery'],check=True,timeout=15)
                if care.FIRMWARE.exists():
                    raise RuntimeError('Kernel interface remained present after unload')
            finally:
                subprocess.run(['modprobe','pou_battery'],check=True,timeout=20)
                subprocess.run(['systemctl','start','pou-battery.service'],check=True,timeout=25)
            wait('fresh-driver-hold',holding,2,4)
            print('UPWARD_CAP_AND_DRIVER_RELOAD_PASSED',flush=True)
            return
        act('battery','full')
        wait('full-current', lambda r: r['phase']=='full' and r['mode']==0 and r['ac'] and
             r['status']=='Charging' and r['watts']>1, 8)
        wait('full-complete', lambda r: r['phase']=='full' and r['capacity']==100 and
             r['status'] in ('Full','Not charging') and r['watts']<0.5, 120)
        # A short maintenance excursion exercises both real boundaries in one session.
        act('battery-limit', '97')
        act('battery','maintain')
        wait('maintenance-discharge', lambda r: r['phase']=='maintenance-drain' and
             r['mode']==2 and not r['ac'] and r['status']=='Discharging' and 0<r['watts']<=12, 5)
        wait('maintenance-target', lambda r: holding(r) and r['capacity']==97, 90, 6)
        print('POLICY_CYCLE_PASSED', flush=True)
    finally:
        with open('/run/lock/pou-control.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            check_owner()
            if original_exists:
                care.atomic(care.CONFIG, original, 0o644)
            else:
                care.CONFIG.unlink(missing_ok=True)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            state=care.status()
            if state.get('phase')=='system' and care.read(care.FIRMWARE/'mode')=='0' and care.read(care.FIRMWARE/'lease_seconds')=='0':
                print('ORIGINAL_SYSTEM_POLICY_RESTORED', flush=True)
                break
            time.sleep(1)
        else:
            raise RuntimeError('Automatic charging restoration not confirmed')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',help='Perform the live test')
    parser.add_argument('--upper-only',action='store_true',help='Test a rising charge ceiling and cold module reload only')
    args=parser.parse_args()
    if args.run:
        from validation_cli import checked
        checked(lambda: run(args.upper_only))
    else:
        parser.print_help()
