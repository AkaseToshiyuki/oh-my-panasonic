#!/usr/bin/env python3
"""Build the optional model-scoped GPL kernel source package."""
from pathlib import Path
import shutil
import subprocess
import tempfile
from deb_metadata import finalize

ROOT = Path(__file__).resolve().parents[1]


def build():
    with tempfile.TemporaryDirectory(prefix='pou-firmware-deb-') as tmp:
        stage = Path(tmp)
        def put(src, dest, mode=0o644):
            path = stage / dest
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / src, path)
            path.chmod(mode)
        def write(dest, data, mode=0o644):
            path = stage / dest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
            path.chmod(mode)
        for name in ('pou_firmware.c', 'pou_fan_control.h', 'pou_battery.c', 'dkms.conf', 'Makefile', 'COPYING'):
            put('kernel/' + name, 'usr/src/pou-firmware-0.7.1/' + name)
        put('packaging/pou-firmware-restore.service', 'usr/lib/systemd/system/pou-firmware-restore.service')
        put('docs/hardware.zh-CN.md', 'usr/share/doc/panasonic-on-ubuntu-firmware-dkms/hardware.zh-CN.md')
        put('kernel/COPYING', 'usr/share/doc/panasonic-on-ubuntu-firmware-dkms/copyright')
        write('usr/lib/modules-load.d/pou-firmware.conf', 'pou_firmware\npou_battery\n')
        write('DEBIAN/control', '''Package: panasonic-on-ubuntu-firmware-dkms
Version: 0.7.2
Architecture: all
Section: kernel
Priority: optional
Maintainer: AkaseToshiyuki <51808852+AkaseToshiyuki@users.noreply.github.com>
Depends: dkms (>= 3.0), oh-my-panasonic (>= 0.20.2)
Recommends: linux-headers-generic
Description: Oh My Panasonic CF-SV7 hardware control modules
 Independent GPL kernel module restricted to CFSV7-3L BIOS V3.11L22.
 Includes a thermal-guarded EC lease and optical driver detach checks.
 Fixed battery commands support leased charge inhibit and online discharge.
''')
        write('DEBIAN/postinst', '''#!/bin/sh
set -eu
if [ "$1" = configure ]; then
    systemctl stop pou-battery.service || true
    systemctl stop pou-power.service || true
    /usr/bin/python3 -I -c 'from panasonic_on_ubuntu.optical import supported,switch; switch("on") if supported() else None' || true
    if ! dkms status -m pou-firmware -v 0.7.1 | grep -q .; then
        dkms add -m pou-firmware -v 0.7.1
    fi
    dkms build -m pou-firmware -v 0.7.1
    if [ -d /sys/module/pou_battery ]; then modprobe -r pou_battery; fi
    if [ -d /sys/module/pou_firmware ]; then modprobe -r pou_firmware; fi
    dkms install -m pou-firmware -v 0.7.1
    if dkms status -m pou-firmware -v 0.1.0 | grep -q .; then
        dkms remove -m pou-firmware -v 0.1.0 --all
    fi
    modprobe pou_firmware
    # The battery module has a narrower SKU gate than other firmware features.
    modprobe pou_battery || true
    systemctl daemon-reload
    systemctl enable pou-firmware-restore.service
    systemctl start pou-firmware-restore.service
    systemctl start pou-power.service
    systemctl start pou-battery.service
    if systemctl is-enabled --quiet pou-optical.service; then systemctl restart pou-optical.service; fi
fi
''', 0o755)
        write('DEBIAN/prerm', '''#!/bin/sh
set -eu
case "$1" in
    upgrade)
        systemctl stop pou-battery.service || true
        systemctl stop pou-power.service || true
        /usr/bin/python3 -I -c 'from panasonic_on_ubuntu.optical import supported,switch; switch("on") if supported() else None' || true
        if [ -d /sys/module/pou_battery ]; then modprobe -r pou_battery; fi
        if [ -d /sys/module/pou_firmware ]; then modprobe -r pou_firmware; fi
        dkms remove -m pou-firmware -v 0.7.1 --all
        ;;
    remove|deconfigure)
        systemctl stop pou-battery.service || true
        systemctl stop pou-optical.service || true
        systemctl stop pou-power.service || true
        /usr/bin/python3 -I -c 'from panasonic_on_ubuntu.optical import supported,switch; switch("on") if supported() else None' || true
        systemctl disable --now pou-firmware-restore.service || true
        if [ -d /sys/module/pou_battery ]; then modprobe -r pou_battery; fi
        if [ -d /sys/module/pou_firmware ]; then
            modprobe -r pou_firmware
        fi
        dkms remove -m pou-firmware -v 0.7.1 --all
        ;;
esac
''', 0o755)
        write('DEBIAN/postrm', '''#!/bin/sh
set -eu
if [ "$1" = purge ]; then
    rm -f /etc/panasonic-on-ubuntu/fan.json
fi
systemctl daemon-reload || true
if [ "$1" = remove ]; then systemctl start pou-power.service pou-battery.service || true; fi
''', 0o755)
        output = ROOT / 'dist'
        finalize(stage)
        output.mkdir(exist_ok=True)
        subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(stage), str(output / 'panasonic-on-ubuntu-firmware-dkms_0.7.2_all.deb')], check=True)


if __name__ == '__main__':
    build()
