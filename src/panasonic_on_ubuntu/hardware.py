"""Install only the validated hardware backend from the common package."""
import os
from pathlib import Path
import subprocess
import sys

DMI = Path('/sys/class/dmi/id')
LOAD = Path('/etc/modules-load.d/oh-my-panasonic.conf')
BACKENDS = {'sv7': ('pou-firmware', '0.8.1'), 'sr3': ('pou-sr3-fan', '0.2.1')}
LEGACY = (('pou-firmware','0.7.1'), ('pou-firmware','0.7.2'), ('pou-firmware','0.8.0'),
          ('pou-sr3-fan','0.1.0'), ('pou-sr3-fan','0.2.0'))


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
    if (identity.get('product_name'), identity.get('product_sku'), identity.get('bios_version')) == ('CFSR3-1', 'CF-SR3SKAKS', 'V1.00L12'):
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


def install():
    backend, modules = selection()
    if backend is None:
        # A disk may have moved from a supported laptop to an unknown model.
        remove()
        return
    name, version = BACKENDS[backend]
    if not registered(name, version):
        run('dkms', 'add', '-m', name, '-v', version)
    kernel = os.uname().release
    run('nice', '-n', '15', 'dkms', 'build', '-m', name, '-v', version, '-k', kernel, '-j', '1')
    unload()
    for old_name, old_version in LEGACY:
        if registered(old_name, old_version):
            run('dkms', 'remove', '-m', old_name, '-v', old_version, '--all')
    for other, (old_name, old_version) in BACKENDS.items():
        if other != backend and registered(old_name, old_version):
            run('dkms', 'remove', '-m', old_name, '-v', old_version, '--all')
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
    if os.geteuid() != 0 or sys.argv[1:] not in (['install'], ['remove']):
        raise SystemExit('Usage (root): python3 -I -m panasonic_on_ubuntu.hardware install|remove')
    (install if sys.argv[1] == 'install' else remove)()
