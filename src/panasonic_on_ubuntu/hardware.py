"""Install only the validated hardware backend from the common package."""
import os
import json
import shutil
import re
from pathlib import Path
import subprocess
import sys

DMI = Path('/sys/class/dmi/id')
LOAD = Path('/etc/modules-load.d/oh-my-panasonic.conf')
BACKENDS = {'sv7': ('pou-firmware', '0.8.3'), 'sr3': ('pou-sr3-fan', '0.2.3')}
LEGACY = (('pou-firmware','0.7.1'), ('pou-firmware','0.7.2'), ('pou-firmware','0.8.0'),
          ('pou-firmware','0.8.1'), ('pou-sr3-fan','0.1.0'), ('pou-sr3-fan','0.2.0'),
          ('pou-sr3-fan','0.2.1'), ('pou-sr3-fan','0.2.2'), ('pou-firmware','0.8.2'))
SR3_BIOS = frozenset(('V1.00L12', 'V1.00L27'))


def supports_sr3(identity):
    return (identity.get('product_name') == 'CFSR3-1'
            and identity.get('product_sku') == 'CF-SR3SKAKS'
            and identity.get('bios_version') in SR3_BIOS)


def selection(identity=None):
    if identity is None:
        identity = {}
        for name in ('product_name', 'product_sku', 'bios_version'):
            try:
                identity[name] = (DMI/name).read_text().strip()
            except OSError:
                identity[name] = ''
    if identity.get('product_name') == 'CFSV7-3L' and identity.get('bios_version') == 'V3.11L22':
        modules = ['pou_firmware']
        if identity.get('product_sku') == 'CF-SV7HD9VS':
            modules.append('pou_battery')
        return 'sv7', modules
    if supports_sr3(identity):
        return 'sr3', ['pou_sr3_fan', 'pou_battery']
    return None, []


def run(*args):
    return subprocess.run(args, check=True, text=True)


def registered(name, version):
    return bool(subprocess.check_output(['dkms', 'status', '-m', name, '-v', version], text=True).strip())


def unload():
    for module in ('pou_battery', 'pou_firmware', 'pou_sr3_fan'):
        if (Path('/sys/module')/module).exists():
            run('rmmod', module)


def kernel_targets(running, boot=Path('/boot'), modules=Path('/lib/modules')):
    targets = [running]
    for image in sorted(boot.glob('vmlinuz-*')):
        release = image.name.removeprefix('vmlinuz-')
        if release != running and image.is_file() and (modules/release/'build'/'Makefile').is_file():
            targets.append(release)
    return targets


TX = Path('/var/lib/oh-my-panasonic/driver-upgrade')
SOURCES = Path('/usr/src')


def restore_sources():
    for name, version in (*BACKENDS.values(), *LEGACY):
        dest = SOURCES / f'{name}-{version}'
        backup = TX / dest.name
        if backup.is_dir() and not dest.exists():
            shutil.copytree(backup, dest)


def installed_kernels(name,version):
    if not registered(name,version):
        return []
    rows = subprocess.check_output(['dkms','status','-m',name,'-v',version],text=True)
    result = []
    for row in rows.splitlines():
        match = re.fullmatch(re.escape(f'{name}/{version}')+r', ([\w.+-]+), [\w-]+: installed(?:\s.*)?',row)
        if match:
            result.append(match[1])
    return result


def rollback():
    restore_sources()
    manifest = TX/'switch.json'
    if not manifest.is_file():
        return
    data = json.loads(manifest.read_text())
    name,version = data['candidate']
    if (name,version) not in BACKENDS.values():
        raise ValueError('未知的恢复事务')
    added = set(installed_kernels(name,version)) - set(data['previous_candidate'])
    if os.uname().release in added:
        unload()
    for kernel in data['kernels']:
        if kernel in added:
            run('dkms','uninstall','-m',name,'-v',version,'-k',kernel)
    for old, old_version, kernel in data['old']:
        if kernel not in installed_kernels(old,old_version):
            run('dkms','install','--force','-m',old,'-v',old_version,'-k',kernel)
    if data['autoload'] is None:
        LOAD.unlink(missing_ok=True)
    else:
        LOAD.write_text(data['autoload'])
    for module in data['loaded']:
        run('modprobe',module)
    manifest.unlink()


def finish():
    if not TX.is_dir():
        return
    # Configuration and service startup have succeeded. Never remove a working
    # candidate as a consequence of a later failure to retire old sources.
    (TX/'switch.json').unlink(missing_ok=True)
    for name,version in LEGACY:
        source = SOURCES/f'{name}-{version}'
        if not (TX/source.name).is_dir() or installed_kernels(name,version):
            continue
        if registered(name,version):
            try:
                run('dkms','remove','-m',name,'-v',version,'--all')
            except subprocess.CalledProcessError as exc:
                print(f'保留旧驱动源码: {exc}', file=sys.stderr)
                continue
        if source.is_dir():
            shutil.rmtree(source)
    shutil.rmtree(TX)


def install():
    restore_sources()
    backend, modules = selection()
    if backend is None:
        # A disk may have moved from a supported laptop to an unknown model.
        remove()
        return
    name, version = BACKENDS[backend]
    if (TX/'switch.json').exists():
        rollback()
    existing = registered(name, version)
    kernels = kernel_targets(os.uname().release)
    try:
        if not existing:
            run('dkms', 'add', '-m', name, '-v', version)
        for kernel in kernels:
            run('nice', '-n', '15', 'dkms', 'build', '-m', name, '-v', version, '-k', kernel, '-j', '1')
    except Exception:
        if not existing and registered(name,version):
            run('dkms','remove','-m',name,'-v',version,'--all')
        raise
    present = installed_kernels(name,version)
    old = [(n,v,k) for n,v in (*LEGACY,*BACKENDS.values()) if (n,v)!=(name,version)
           for k in installed_kernels(n,v) if k in kernels]
    if old or any(k not in present for k in kernels):
        TX.mkdir(parents=True,mode=0o700,exist_ok=True)
        data = dict(candidate=(name,version), previous_candidate=present,kernels=kernels,old=old,
                    loaded=[m for m in ('pou_firmware','pou_sr3_fan','pou_battery') if (Path('/sys/module')/m).exists()],
                    autoload=LOAD.read_text() if LOAD.is_file() else None)
        from .care import atomic
        atomic(TX/'switch.json',data)
        unload()
        for n,v,k in old:
            run('dkms','uninstall','-m',n,'-v',v,'-k',k)
        for kernel in kernels:
            if kernel not in present:
                run('dkms', 'install', '-m', name, '-v', version, '-k', kernel)
    for module in modules:
        run('modprobe', module)
    LOAD.parent.mkdir(parents=True, exist_ok=True)
    LOAD.write_text('\n'.join(modules)+'\n')
    LOAD.chmod(0o644)
    run('systemctl', 'enable', 'pou-firmware-restore.service')
    run('systemctl', 'restart', 'pou-firmware-restore.service')


def remove():
    unload()
    for name, version in (*BACKENDS.values(), *LEGACY):
        if registered(name, version):
            run('dkms', 'remove', '-m', name, '-v', version, '--all')
    LOAD.unlink(missing_ok=True)
    run('systemctl', 'disable', '--now', 'pou-firmware-restore.service')


if __name__ == '__main__':
    actions = {'install':install,'remove':remove,'rollback':rollback,'finish':finish}
    if os.geteuid() != 0 or len(sys.argv) != 2 or sys.argv[1] not in actions:
        raise SystemExit('Usage (root): python3 -I -m panasonic_on_ubuntu.hardware install|remove')
    actions[sys.argv[1]]()
