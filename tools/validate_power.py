"""Explicit, bounded on-device load check. Run as root on the validated SV7."""
import hashlib
import argparse
import json
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import time


def load(seconds):
    os.nice(15)
    end = time.monotonic() + seconds
    block = b'Panasonic test' * 4096
    while time.monotonic() < end:
        hashlib.sha256(block).digest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run',action='store_true',help='Run the live CPU load test')
    parser.add_argument('--mode',choices=['eco','balanced','performance','adaptive','boost'],default='balanced')
    parser.add_argument('--seconds',type=int,default=30)
    parser.add_argument('--workers',type=int,choices=range(1,5),default=2)
    args=parser.parse_args()
    if not 10<=args.seconds<=60:parser.error('Duration must be 10..60 seconds')
    if not args.run:
        parser.print_help(); return
    original = json.loads(Path('/etc/panasonic-on-ubuntu/power.json').read_text()).get('mode', 'system')
    report = []
    workers = []
    output=Path('/var/log/panasonic-on-ubuntu')/f'validation-{args.mode}-{int(time.time())}.jsonl'
    output.parent.mkdir(mode=0o750,parents=True,exist_ok=True)
    def record(value):
        with output.open('a') as f:
            f.write(json.dumps(value)+'\n');f.flush();os.fsync(f.fileno())
    try:
        for mode, duration in [(args.mode,args.seconds)]:
            record({'event':'start','mode':mode,'duration':duration,'workers':args.workers,'timestamp':time.time()})
            subprocess.run(['/usr/libexec/pou-control','cpu',mode], check=True, timeout=30)
            workers = [mp.Process(target=load,args=(duration,)) for _ in range(args.workers)]
            for p in workers:
                p.start()
            samples = []
            start = time.monotonic()
            while time.monotonic() - start < duration:
                time.sleep(2)
                s = json.loads(Path('/run/panasonic-on-ubuntu/power.json').read_text())
                samples.append(s)
                record({k:v for k,v in s.items() if k!='history'})
                if time.time()-s['timestamp']>8 or not s.get('sample') or s['sample']['temperature']>=85:
                    raise RuntimeError('Monitor stale or high temperature; ending load')
            for p in workers:
                p.join(timeout=3)
            workers = []
            report.append({'mode':mode,'samples':samples})
            last = [s['sample'] for s in samples[-10:] if s.get('sample')]
            print(mode, 'last samples watts', [s['watts'] for s in last], 'peak temperature', max(s['sample']['temperature'] for s in samples if s.get('sample')), flush=True)
    finally:
        for p in workers:
            p.terminate();p.join(timeout=3)
        subprocess.run(['/usr/libexec/pou-control','cpu',original],check=False,timeout=30)
        record({'event':'finished','restored':json.loads(Path('/run/panasonic-on-ubuntu/power.json').read_text()).get('mode')==original,'timestamp':time.time()})
        print('RESULT',output,flush=True)


if __name__ == '__main__':
    from validation_cli import checked
    checked(main)
