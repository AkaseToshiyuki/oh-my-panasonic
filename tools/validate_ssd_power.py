"""Bounded PS0/PS1/PS2 file-I/O validation with an independent timed rollback."""
import argparse
import json
import mmap
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import uuid


def restore(folder):
    from panasonic_on_ubuntu.ssd_power import Device
    folder = folder.resolve()
    info = folder.stat()
    if (folder.parent != Path('/var/tmp') or not folder.name.startswith('pou-ssd-verify-')
            or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('Invalid recovery directory')
    before = json.loads((folder/'original.json').read_text())
    device = Device(before['device'])
    device.write(before['value'], before['fingerprint'])


def run():
    from panasonic_on_ubuntu.ssd_power import Device
    if os.geteuid() != 0: raise ValueError('Run as root')
    if subprocess.run(['systemctl','is-active','--quiet','pou-power.service']).returncode == 0:
        raise ValueError('先停止 pou-power.service，验证结束后再启动，避免与日常策略争用 SSD')
    device = Device()
    before = device.read(wake=True)
    unit = 'pou-ssd-rollback-' + uuid.uuid4().hex[:8]
    records = []
    directory = tempfile.mkdtemp(prefix='pou-ssd-verify-',dir='/var/tmp')
    folder = Path(directory)
    try:
        (folder/'original.json').write_text(json.dumps(before))
        subprocess.run(['systemd-run','--quiet','--unit',unit,'--on-active=35s','--collect',
            '--setenv=PYTHONPATH='+str(Path(__file__).resolve().parents[1]/'src'),
            '/usr/bin/python3',str(Path(__file__).resolve()),'--restore',directory],check=True)
        try:
            descriptor = os.open(folder/'blocks', os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_DIRECT, 0o600)
            try:
                pattern = os.urandom(1024*1024)
                with mmap.mmap(-1,len(pattern)) as block:
                    for selected in (1,2,0):
                        value = (before['value'] & ~31) | selected
                        after = device.write(value,before['fingerprint'])
                        assert after['apst_hash'] == before['apst_hash']
                        block[:] = pattern
                        for index in range(16):
                            assert os.pwritev(descriptor,[block],index*len(pattern)) == len(pattern)
                        os.fsync(descriptor)
                        for index in range(16):
                            assert os.preadv(descriptor,[block],index*len(pattern)) == len(pattern)
                            assert block[:] == pattern
                        time.sleep(2.5)
                        idle = device.read()
                        active = device.read(wake=True)
                        assert active['selected'] == selected, active
                        assert active['apst_hash'] == before['apst_hash'], active
                        records.append(dict(requested=selected,after_idle=idle['selected'],
                            after_wake=active['selected'],direct_io_verified_mib=16,apst_unchanged=True))
            finally: os.close(descriptor)
        finally:
            restore(folder)
            subprocess.run(['systemctl','stop',unit+'.timer'],check=True)
        final = device.read(wake=True)
        assert final['value'] == before['value'] and final['apst_hash'] == before['apst_hash']
        print(json.dumps({'results':records,'original_restored':True,'original_state':before['selected']},indent=2))
    except BaseException:
        # Retain the recovery snapshot if restoration failed; the independent
        # timer still needs it. Failed validation evidence must not disappear.
        raise
    else:
        shutil.rmtree(folder)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--restore',type=Path,help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.run or args.restore:
        from validation_cli import checked
        checked((lambda:restore(args.restore)) if args.restore else run)
    else: parser.print_help()
