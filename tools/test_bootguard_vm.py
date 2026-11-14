#!/usr/bin/env python3
"""Run boot transactions only inside bounded KVM guests; defaults to help."""
from pathlib import Path
import argparse,dataclasses,json,shutil,subprocess,sys,tempfile,time
from build_bootguard import build,build_image,ROOT
sys.path.insert(0,str(ROOT/'src'))
from panasonic_on_ubuntu.boot_unlock import BootState,Backup,Phase,candidate

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',action='store_true');p.add_argument('--sdk');p.add_argument('--case');a=p.parse_args()
    if not a.run:p.print_help();return
    if not Path('/dev/kvm').exists():raise RuntimeError('KVM required; no host firmware calls are allowed')
    guard=build(a.sdk);out=ROOT/'build'
    launcher=build_image(ROOT/'tests/bootguard_vm/launcher.c',out,'guard-vm-launcher',a.sdk,('-Wno-unused-function',))
    shim=build_image(ROOT/'tests/bootguard_vm/shim.c',out,'guard-vm-shim',a.sdk,('-Wno-unused-function',))
    root=Path(tempfile.mkdtemp(prefix='bootguard-vm-',dir=out));results=[]
    names=('help','enable-ack','no-ack','requested-restore','denied-write','denied-restore','corrupt-state','bad-backup','wrong-size','unknown-bios','secure-boot')
    if a.case:
        if a.case not in names:raise ValueError('Unknown VM case')
        names=(a.case,)
    for name in names:
        case=root/name;esp=case/'esp';dest=esp/'EFI/OhMyPanasonic35';dest.mkdir(parents=True)
        for d in ('BOOT','ubuntu'):(esp/'EFI'/d).mkdir()
        shutil.copyfile(launcher,esp/'EFI/BOOT/BOOTX64.EFI');shutil.copyfile(shim,esp/'EFI/ubuntu/shimx64.efi')
        shutil.copyfile(guard,dest/'guard.efi');shutil.copyfile('/usr/share/OVMF/OVMF_VARS_4M.fd',case/'vars.fd')
        state_path=dest/'state.bin';state_path.write_bytes(BootState(Phase.ENABLE,0,0,'CF-SR3SKAKS','V1.00L27').encode())
        if name in ('help','wrong-size','secure-boot'):(esp/(name+'.flag')).touch()
        if name=='denied-write':(esp/'deny-candidate.flag').touch()
        boots=[]
        def boot(expected):
            index=len(boots);start=time.monotonic()
            command=['systemd-run','--user','--scope','--quiet','-p','CPUQuota=25%','-p','MemoryMax=768M',
                'timeout','50','qemu-system-x86_64','-accel','kvm','-cpu','host','-smp','1','-m','256',
                '-smbios','type=0,version='+('V1.00L28' if name=='unknown-bios' else 'V1.00L27'),
                '-smbios','type=1,manufacturer=Panasonic,product=CFSR3-1,sku=CF-SR3SKAKS',
                '-drive','if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd',
                '-drive','if=pflash,format=raw,file='+str(case/'vars.fd'),
                '-drive','if=virtio,format=raw,file=fat:rw:'+str(esp),'-nic','none','-display','none','-monitor','none',
                '-serial','file:'+str(case/f'serial-{index}.log'),'-device','isa-debug-exit,iobase=0xf4,iosize=0x04','-no-reboot']
            with (case/f'process-{index}.log').open('w') as log:
                r=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=60)
            boots.append({'exit':r.returncode,'seconds':round(time.monotonic()-start,3)})
            if (dest/'last-boot.txt').exists():shutil.copyfile(dest/'last-boot.txt',case/f'report-{index}.txt')
            if r.returncode!=expected:
                print((case/f'serial-{index}.log').read_text(errors='replace')[-6000:])
                raise RuntimeError((name,index,r.returncode,expected,str(case)))
        if name in ('help','wrong-size','unknown-bios','secure-boot'):
            boot(33);assert not (dest/'original.bin').exists()
            assert (dest/'chain.ok').exists()==(name!='help')
        elif name=='denied-write':
            boot(33);assert BootState.decode(state_path.read_bytes()).phase==Phase.DISABLED
        else:
            boot(0);state=BootState.decode(state_path.read_bytes());assert state.phase==Phase.APPLY_PENDING
            backup=Backup.decode((dest/'original.bin').read_bytes());state.validate_backup(backup)
            boot(33);state=BootState.decode(state_path.read_bytes());assert state.phase==Phase.BOOT_PENDING
            assert (esp/'cpu-before.bin').read_bytes()==candidate(backup.cpu)
            if name=='enable-ack':
                state_path.write_bytes(dataclasses.replace(state,phase=Phase.ACTIVE).encode())
                boot(33);later=BootState.decode(state_path.read_bytes())
                assert later.phase==Phase.BOOT_PENDING and later.generation==state.generation+1
            elif name=='bad-backup':
                damaged=bytearray((dest/'original.bin').read_bytes());damaged[100]^=1
                (dest/'original.bin').write_bytes(damaged);boot(33)
                assert BootState.decode(state_path.read_bytes()).phase==Phase.BOOT_PENDING
            else:
                if name=='requested-restore':state_path.write_bytes(dataclasses.replace(state,phase=Phase.RESTORE).encode())
                if name=='denied-restore':(esp/'deny-restore.flag').touch()
                if name=='corrupt-state':state_path.write_bytes(b'broken-state')
                boot(33 if name=='denied-restore' else 0)
                expected=Phase.RESTORE if name=='denied-restore' else Phase.DISABLED
                assert BootState.decode(state_path.read_bytes()).phase==expected
                if name!='denied-restore':
                    boot(33);assert (esp/'cpu-before.bin').read_bytes()==backup.cpu
        row={'case':name,'boots':boots,'passed':True};results.append(row);print(json.dumps(row),flush=True)
        (root/'results.json').write_text(json.dumps(results,indent=2))
    print('VM results: '+str(root),flush=True)

if __name__=='__main__':main()
