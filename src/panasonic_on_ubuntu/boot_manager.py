"""Provision, initialize and recover the optional SR3 boot power guard."""
from pathlib import Path
import argparse,dataclasses,hashlib,json,os,re,struct,subprocess,time
from .boot_unlock import Backup,BootState,Phase,load_records,supported

ESP=Path('/boot/efi')
DIRECTORY=ESP/'EFI/OhMyPanasonic35'
STORE=Path('/var/lib/oh-my-panasonic/boot-unlock')
RUNTIME=Path('/run/panasonic-on-ubuntu')
VARIABLES=Path('/sys/firmware/efi/efivars')
GLOBAL='8be4df61-93ca-11d2-aa0d-00e098032b8c'
IMAGE=Path('/usr/lib/oh-my-panasonic/pou-bootguard.efi')
RAPL=Path('/sys/class/powercap/intel-rapl-mmio:0')
LOADER='\\EFI\\OhMyPanasonic35\\guard.efi'
DESCRIPTION='Oh My Panasonic 35W'

def run(*args):
    return subprocess.run(args,check=True,text=True,capture_output=True,timeout=40).stdout

def atomic(path,data,mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.new')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'wb') as f:
        os.fchmod(f.fileno(),mode)
        f.write(data);f.flush();os.fsync(f.fileno())
    temp.replace(path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def record(path,value,mode=0o600):
    atomic(path,json.dumps(value,ensure_ascii=False,sort_keys=True).encode(),mode)

def variable(name):
    return (VARIABLES/(name+'-'+GLOBAL)).read_bytes()[4:]

def identity():
    root=Path('/sys/class/dmi/id')
    data={name:(root/name).read_text().strip() for name in ('product_name','product_sku','bios_version')}
    if data['product_name']!='CFSR3-1' or not supported(data['product_sku'],data['bios_version']):
        raise RuntimeError('此机型或固件尚不支持启动功耗调整')
    if 'i5-1245U' not in Path('/proc/cpuinfo').read_text():
        raise RuntimeError('处理器不在已验证范围')
    return data

def msr_limit():
    path=Path('/dev/cpu/0/msr')
    if not path.exists():run('modprobe','msr')
    with path.open('rb',buffering=0) as f:f.seek(0x610);return int.from_bytes(f.read(8),'little')

def boot_id():return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

def mount_device():
    fields=run('findmnt','-no','SOURCE,FSTYPE,TARGET','--target',str(ESP)).split()
    if len(fields)!=3 or fields[1:]!=['vfat',str(ESP)]:raise RuntimeError('EFI 分区挂载不匹配')
    device=Path(fields[0]).resolve();block=Path('/sys/class/block')/device.name
    part=int((block/'partition').read_text());parent=block.resolve().parent.name
    if not re.fullmatch(r'[A-Za-z0-9_-]+',parent) or not (Path('/dev')/parent).is_block_device():
        raise RuntimeError('无法确认 EFI 分区所属磁盘')
    if not (ESP/'EFI/ubuntu/shimx64.efi').is_file():raise RuntimeError('缺少原 Ubuntu 启动程序')
    return str(Path('/dev')/parent),str(part)

def preflight():
    data=identity();mount_device()
    if variable('SecureBoot')!=b'\0':raise RuntimeError('当前需要关闭状态的 Secure Boot；不会自动修改此设置')
    run('systemctl','is-active','--quiet','pou-power','pou-battery','thermald')
    return data

def state_checked():
    info=identity();state,backup=load_records(DIRECTORY)
    if (state.sku,state.bios)!=(info['product_sku'],info['bios_version']):
        raise RuntimeError('固件身份已变化，未执行启动功耗写入')
    return state,backup

def config():return json.loads((STORE/'installation.json').read_text())

def publish(phase,error=''):
    RUNTIME.mkdir(parents=True,exist_ok=True,mode=0o755)
    RUNTIME.chmod(0o755)
    record(RUNTIME/'boot-unlock.json',{'timestamp':time.time(),'phase':phase,'error':error},0o644)

def enable():
    info=preflight()
    if msr_limit()!=0x804281b800dd80e0:raise RuntimeError('请先恢复原始 28/55W 处理器上限')
    if DIRECTORY.exists() or (STORE/'enabled').exists():raise RuntimeError('存在启动事务，请先完成恢复')
    if (VARIABLES/('BootNext-'+GLOBAL)).exists():raise RuntimeError('已有其他待执行启动项')
    image=IMAGE.read_bytes()
    if not image.startswith(b'MZ'):raise RuntimeError('启动程序无效')
    disk,part=mount_device();original_order=variable('BootOrder')
    if len(original_order)%2 or len(original_order)>128:raise RuntimeError('启动顺序格式不受支持')
    current=struct.unpack('<H',variable('BootCurrent'))[0]
    current_blob=variable(f'Boot{current:04X}')
    if '\\EFI\\ubuntu\\shimx64.efi'.lower().encode('utf-16le') not in current_blob.lower():
        # ASCII lowercasing is valid for this fixed UTF-16LE path.
        raise RuntimeError('当前启动项不是已验证的 Ubuntu shim')
    DIRECTORY.mkdir(mode=0o700);STORE.mkdir(parents=True,exist_ok=True,mode=0o700);STORE.chmod(0o700)
    number=None
    try:
        atomic(DIRECTORY/'guard.efi',image)
        state=BootState(Phase.ENABLE,0,0,info['product_sku'],info['bios_version'])
        atomic(DIRECTORY/'state.bin',state.encode())
        options=STORE/'options.bin';atomic(options,('--guard\0').encode('utf-16le'))
        before=set(VARIABLES.glob('Boot????-'+GLOBAL))
        run('efibootmgr','-C','-d',disk,'-p',part,'-L',DESCRIPTION,'-l',LOADER,'-@',str(options))
        created=set(VARIABLES.glob('Boot????-'+GLOBAL))-before
        if len(created)!=1:raise RuntimeError('无法确定新建的启动项')
        number=int(created.pop().name[4:8],16)
        blob=variable(f'Boot{number:04X}')
        if LOADER.encode('utf-16le') not in blob or not blob.endswith(options.read_bytes()):
            raise RuntimeError('启动项读回不一致')
        record(STORE/'installation.json',{'identity':info,'entry':number,'original_order':original_order.hex(),
            'image_sha256':hashlib.sha256(image).hexdigest(),'installed_at':time.time()})
        atomic(STORE/'enabled',b'1\n')
        order=[number,*struct.unpack('<'+'H'*(len(original_order)//2),original_order)]
        run('efibootmgr','-o',','.join(f'{n:04X}' for n in order))
        run('efibootmgr','-n',f'{number:04X}')
        run('systemctl','enable','pou-boot-power-init.service','pou-boot-power-confirm.timer')
        publish('pending');print('已准备 35W 启动配置；下次启动会自动重启一次，然后等待系统健康确认。')
    except BaseException:
        if number is not None:
            try:
                if variable('BootNext')==struct.pack('<H',number):run('efibootmgr','-N')
            except FileNotFoundError:pass
            run('efibootmgr','-o',','.join(f'{n:04X}' for n in struct.unpack('<'+'H'*(len(original_order)//2),original_order)))
            run('efibootmgr','-b',f'{number:04X}','-B')
        (STORE/'enabled').unlink(missing_ok=True)
        for name in ('guard.efi','state.bin'):(DIRECTORY/name).unlink(missing_ok=True)
        DIRECTORY.rmdir()
        raise

def save_phase(state,phase):
    atomic(DIRECTORY/'state.bin',dataclasses.replace(state,phase=phase).encode())

def disable():
    # Recovery must remain available when a service health check has failed.
    identity();mount_device();entry=config()['entry']
    staged=BootState.decode((DIRECTORY/'state.bin').read_bytes())
    if staged.phase==Phase.ENABLE and not (DIRECTORY/'original.bin').exists():
        if msr_limit()!=0x804281b800dd80e0:raise RuntimeError('处理器已不是原始上限，保留恢复文件')
        remove_entry(entry)
        for name in ('guard.efi','state.bin','last-boot.txt'):(DIRECTORY/name).unlink(missing_ok=True)
        DIRECTORY.rmdir();(STORE/'enabled').unlink(missing_ok=True);publish('disabled');return
    state,backup=state_checked()
    if state.phase==Phase.DISABLED and msr_limit()==0x804281b800dd80e0:return cleanup()
    try:
        existing=variable('BootNext')
        if existing!=struct.pack('<H',entry):raise RuntimeError('已有其他待执行启动项')
    except FileNotFoundError:pass
    save_phase(state,Phase.RESTORE)
    run('efibootmgr','-n',f'{entry:04X}');publish('restore-pending')
    print('已安排恢复原始功耗上限；下次启动会恢复并自动重启一次。')

def remove_entry(entry):
    name=f'Boot{entry:04X}'
    try:
        blob=variable(name)
        if LOADER.encode('utf-16le') not in blob:raise RuntimeError('启动项已被其他程序修改')
        try:
            if variable('BootNext')==struct.pack('<H',entry):run('efibootmgr','-N')
        except FileNotFoundError:pass
        run('efibootmgr','-b',f'{entry:04X}','-B')
    except FileNotFoundError:pass

def cleanup():
    state,backup=state_checked()
    if state.phase!=Phase.DISABLED or msr_limit()!=0x804281b800dd80e0:
        raise RuntimeError('固件功耗尚未恢复，保留恢复文件')
    remove_entry(config()['entry'])
    atomic(STORE/'original.bin',backup.encode())
    (STORE/'enabled').unlink(missing_ok=True)
    for name in ('guard.efi','state.bin','original.bin','last-boot.txt'):
        (DIRECTORY/name).unlink(missing_ok=True)
    if not any(DIRECTORY.iterdir()):DIRECTORY.rmdir()
    (RUNTIME/'boot-power-envelope.json').unlink(missing_ok=True)
    publish('disabled')

def normalize_mmio(base):
    names=('enabled','constraint_0_power_limit_uw','constraint_1_power_limit_uw')
    before={n:(base/n).read_text().strip() for n in names}
    target=dict(zip(names,('1','28000000','55000000')))
    if before==target:return {'before':before,'after':before,'changed':False}
    if before['enabled']!='0' or before[names[1]] not in ('0','35000000') or before[names[2]]!='0':
        raise RuntimeError('启动 MMIO 初值不在已验证范围')
    window_paths=[base/f'constraint_{i}_time_window_us' for i in (0,1)]
    windows=[p.read_text().strip() for p in window_paths]
    if windows!=['976','976']:raise RuntimeError('启动时间窗口不在已验证范围')
    try:
        for name in names[1:]: (base/name).write_text(target[name]+'\n')
        for p,value in zip(window_paths,('27983872','1952')):p.write_text(value+'\n')
        (base/'enabled').write_text('1\n')
        if any((base/n).read_text().strip()!=target[n] for n in names):raise RuntimeError('初始化功耗读回失败')
        if [p.read_text().strip() for p in window_paths]!=['27983872','1952']:raise RuntimeError('时间窗口读回失败')
    except BaseException:
        # An initialization failure must leave the independent locked MSR
        # limits in force, with no partially enabled MMIO policy.
        errors=[]
        for p,value in [(base/'enabled','0'),*zip(window_paths,windows),
                        *((base/n,before[n]) for n in names[1:])]:
            try:
                p.write_text(value+'\n')
                if p.read_text().strip()!=value:raise RuntimeError('readback differs')
            except (OSError,RuntimeError) as error:errors.append(f'{p.name}: {error}')
        if errors:raise RuntimeError('MMIO 初始化恢复失败：'+'; '.join(errors))
        raise
    return {'before':before,'after':target,'changed':True}

def initialize():
    if not (STORE/'enabled').exists():return
    state,backup=state_checked()
    if state.phase==Phase.DISABLED:return cleanup()
    if state.phase not in (Phase.BOOT_PENDING,Phase.ACTIVE):raise RuntimeError('启动事务尚未进入可用状态')
    if msr_limit()!=0x804281b800dd8118:raise RuntimeError('35/55W 处理器限制未生效')
    atom=RUNTIME/'boot-power-envelope.json'
    if atom.exists():
        old=json.loads(atom.read_text())
        if old.get('boot_id')==boot_id() and old.get('generation')==state.generation:return
    result=normalize_mmio(RAPL)
    if state.phase!=Phase.BOOT_PENDING or variable('BootCurrent')!=struct.pack('<H',config()['entry']):
        raise RuntimeError('本次启动未经过守卫，保留 28W 初始限制')
    atomic(STORE/'original.bin',backup.encode())
    record(atom,{'boot_id':boot_id(),'generation':state.generation,'sku':state.sku,'bios':state.bios,
        'pl1_w':35,'pl2_w':55,'baseline_pl1_w':28,'baseline_pl2_w':55,'initialization':result})
    publish('awaiting-health')

def confirm():
    if not (STORE/'enabled').exists():return
    state,backup=state_checked()
    if state.phase==Phase.DISABLED:return cleanup()
    if state.phase!=Phase.BOOT_PENDING:return
    envelope=json.loads((RUNTIME/'boot-power-envelope.json').read_text())
    if envelope['boot_id']!=boot_id() or envelope['generation']!=state.generation:
        raise RuntimeError('启动健康记录不匹配')
    if msr_limit()!=0x804281b800dd8118:raise RuntimeError('处理器限制发生变化')
    run('systemctl','is-active','--quiet','pou-power','pou-battery','thermald')
    power=json.loads((RUNTIME/'power.json').read_text());care=json.loads((RUNTIME/'battery-care.json').read_text())
    sample=power.get('sample') or {}
    if (not 0<=time.time()-power['timestamp']<10 or not 0<=time.time()-care['timestamp']<10
            or power.get('error') or care.get('error') or power.get('fan',{}).get('error')
            or not 0<=time.monotonic()-sample.get('received',0)<8
            or not 0<sample.get('temperature',0)<85
            or power.get('presets',{}).get('adaptive',{}).get('cutoff_c')!=85):
        raise RuntimeError('系统服务健康检查未通过，下次启动将回退')
    current=BootState.decode((DIRECTORY/'state.bin').read_bytes())
    if current!=state:raise RuntimeError('启动事务在检查期间变化')
    save_phase(state,Phase.ACTIVE);publish('active')

def removal_check():
    if (STORE/'enabled').exists():
        raise RuntimeError('请先运行 sudo pou-boot-power disable --apply 并重启，恢复原始功耗后再卸载。')

def main():
    p=argparse.ArgumentParser(description='Oh My Panasonic 可恢复启动功耗控制')
    p.add_argument('action',choices=('status','enable','disable','initialize','confirm','removal-check'),nargs='?',default='status')
    p.add_argument('--apply',action='store_true');a=p.parse_args()
    if a.action=='status':
        print((RUNTIME/'boot-unlock.json').read_text() if (RUNTIME/'boot-unlock.json').exists() else '{"phase":"disabled"}');return
    if not a.apply:p.print_help();return
    if os.geteuid()!=0:raise SystemExit('需要 root 权限')
    actions={'enable':enable,'disable':disable,'initialize':initialize,'confirm':confirm,'removal-check':removal_check}
    try:actions[a.action]()
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
        publish('error',str(error));raise SystemExit(str(error))

if __name__=='__main__':main()
