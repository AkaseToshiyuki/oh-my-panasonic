#!/usr/bin/env python3
"""Build the main Debian package and its compatibility transition package on Ubuntu."""
from pathlib import Path
import shutil
import subprocess
import tempfile
from deb_metadata import finalize
from collect_rust_licenses import collect
from rust_build import environment

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.20.7'

def build():
    wheelpad = ROOT/'vendor/letsnote-wheelpad'
    env = environment(ROOT, 'oh-my-panasonic')
    # Keep the package input at a fixed location even when the caller uses a shared target cache.
    env['CARGO_TARGET_DIR'] = str(wheelpad/'target')
    subprocess.run(['cargo', 'build', '--release', '--locked'], cwd=wheelpad, env=env, check=True)
    with tempfile.TemporaryDirectory(prefix='pou-deb-') as tmp:
        stage = Path(tmp)
        collect(ROOT/'vendor/letsnote-wheelpad', 'letsnote-wheelpad', stage/'usr/share/doc/panasonic-on-ubuntu/rust-licenses/wheelpad')
        pose_licenses = ROOT/'build/pose-licenses'
        if not (pose_licenses/'SOURCES.txt').is_file():
            raise RuntimeError('Build the pose helper with tools/build_pose_helper.py first')
        shutil.copytree(pose_licenses, stage/'usr/share/doc/panasonic-on-ubuntu/rust-licenses/pose')
        def put(src, dest, mode=0o644):
            dst = stage / dest
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / src, dst)
            dst.chmod(mode)
        for source in (ROOT / 'src/panasonic_on_ubuntu').glob('*.py'):
            put(source, 'usr/lib/python3/dist-packages/panasonic_on_ubuntu/' + source.name)
        put('build/pou-pose-verify','usr/libexec/pou-pose-verify',0o755)
        put('vendor/letsnote-wheelpad/target/release/letsnote-wheelpad', 'usr/libexec/pou-wheelpad', 0o755)
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        str(ROOT / 'src/face-session.c'), '-lsystemd', '-o',
                        str(stage / 'usr/libexec/pou-face-session')], check=True)
        (stage/'usr/libexec/pou-face-session').chmod(0o755)
        for name in ['pou-control', 'pou-adjust', 'pou-input-check', 'pou-face-manage', 'pou-face-check', 'pou-face-authorize']:
            put('packaging/' + name, 'usr/libexec/' + name, 0o755)
        for name in ['pou-wheelpad.service', 'pou-power.service', 'pou-optical.service', 'pou-battery.service', 'pou-face-broker.socket', 'pou-face-broker@.service']:
            put('packaging/' + name, 'usr/lib/systemd/system/' + name)
        for name in ['pou-monitor.service', 'pou-monitor.timer', 'pou-tray.service']:
            put('packaging/' + name, 'usr/lib/systemd/user/' + name)
        put('packaging/71-pou-input.rules', 'usr/lib/udev/rules.d/71-pou-input.rules')
        put('packaging/io.github.panasonic_on_ubuntu.desktop', 'usr/share/applications/io.github.panasonic_on_ubuntu.desktop')
        put('packaging/pou-indicator.desktop', 'usr/share/applications/pou-indicator.desktop')
        for source in (ROOT / 'extension').iterdir():
            if source.is_file():
                put(source, 'usr/share/gnome-shell/extensions/panasonic-on-ubuntu@local/' + source.name)
        put('packaging/pou-tray.desktop', 'etc/xdg/autostart/pou-tray.desktop')
        put('packaging/pou-optical-sleep','usr/lib/systemd/system-sleep/pou-optical',0o755)
        put('packaging/pou-power-sleep','usr/lib/systemd/system-sleep/pou-power',0o755)
        put('packaging/pou-battery-sleep','usr/lib/systemd/system-sleep/pou-battery',0o755)
        put('assets/pou-symbolic.svg', 'usr/share/icons/hicolor/scalable/status/pou-symbolic.svg')
        put('packaging/io.github.panasonic_on_ubuntu.policy', 'usr/share/polkit-1/actions/io.github.panasonic_on_ubuntu.policy')
        put('packaging/50-pou-adjust.rules', 'usr/share/polkit-1/rules.d/50-pou-adjust.rules')
        put('packaging/io.github.panasonic_on_ubuntu.face.policy', 'usr/share/polkit-1/actions/io.github.panasonic_on_ubuntu.face.policy')
        for name in ('README.md', 'CHANGELOG.md', 'SECURITY.md', 'CONTRIBUTING.md'):
            put(name, 'usr/share/doc/panasonic-on-ubuntu/' + name)
        for name in ('usage.zh-CN.md', 'build.zh-CN.md', 'hardware.zh-CN.md'):
            put('docs/' + name, 'usr/share/doc/panasonic-on-ubuntu/docs/' + name)
        put('LICENSE', 'usr/share/doc/panasonic-on-ubuntu/copyright')
        put('LICENSE', 'usr/share/doc/panasonic-on-ubuntu/LICENSE')
        put('kernel/COPYING', 'usr/share/doc/panasonic-on-ubuntu/kernel/COPYING')
        put('vendor/README.md', 'usr/share/doc/panasonic-on-ubuntu/vendor/README.md')
        put('vendor/Facelock-LICENSE-MIT', 'usr/share/doc/panasonic-on-ubuntu/vendor/Facelock-LICENSE-MIT')
        put('vendor/letsnote-wheelpad/LICENSE', 'usr/share/doc/panasonic-on-ubuntu/vendor/letsnote-wheelpad/LICENSE')
        def write(dest, text, mode=0o644):
            p = stage / dest
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            p.chmod(mode)
        write('usr/bin/pou', '#!/usr/bin/python3\nfrom panasonic_on_ubuntu.cli import main\nmain()\n', 0o755)
        write('usr/lib/sysusers.d/pou-input.conf', 'u pou-input - "Panasonic touchpad service" /nonexistent /usr/sbin/nologin\n')
        write('usr/lib/modules-load.d/pou-input.conf', 'uinput\n')
        write('DEBIAN/control', f'''Package: oh-my-panasonic
Version: {VERSION}
Section: utils
Priority: optional
Architecture: amd64
Maintainer: AkaseToshiyuki <51808852+AkaseToshiyuki@users.noreply.github.com>
Depends: python3 (>= 3.10), gir1.2-gstreamer-1.0, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, python3-gi, python3-gi-cairo, python3-evdev, gir1.2-gtk-4.0, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, gir1.2-adw-1, gir1.2-nm-1.0, pulseaudio-utils, power-profiles-daemon, upower, polkitd, pkexec, systemd, udev, libsystemd0, libc6 (>= 2.39), libgcc-s1, libtss2-esys-3.0.2-0t64, libtss2-mu-4.0.1-0t64, libtss2-tctildr0t64
Recommends: network-manager, wireplumber, bluez, gnome-control-center, linux-tools-generic, iw
Suggests: panasonic-on-ubuntu-firmware-dkms, facelock, panasonic-on-ubuntu-face-gpu
Conflicts: letsnote-wheelpad
Replaces: panasonic-on-ubuntu (<< 0.20.0)
Description: Oh My Panasonic laptop control center
 Card dashboard, native GNOME controls, hardware-counter monitoring,
 guarded CPU power presets, optical power and a CF-SV7 WheelPad backend.
 Model-scoped charge limits and maintenance use an expiring kernel lease.
''')
        for name in ['postinst', 'prerm', 'postrm']:
            put('packaging/' + name, 'DEBIAN/' + name, 0o755)
        write('DEBIAN/conffiles', '/etc/xdg/autostart/pou-tray.desktop\n')
        finalize(stage)
        output = ROOT / 'dist'
        output.mkdir(exist_ok=True)
        subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(stage), str(output / f'oh-my-panasonic_{VERSION}_amd64.deb')], check=True)
    # The empty transition package upgrades the old identity without running its
    # remove/purge hooks. Existing optional components keep their dependency.
    with tempfile.TemporaryDirectory(prefix='omp-transition-') as tmp:
        stage=Path(tmp);(stage/'DEBIAN').mkdir()
        (stage/'DEBIAN/control').write_text(f'''Package: panasonic-on-ubuntu
Version: {VERSION}
Section: oldlibs
Priority: optional
Architecture: all
Maintainer: AkaseToshiyuki <51808852+AkaseToshiyuki@users.noreply.github.com>
Depends: oh-my-panasonic (= {VERSION})
Description: Compatibility transition to Oh My Panasonic
 This empty package preserves upgrades and optional component dependencies.
''')
        finalize(stage)
        subprocess.run(['dpkg-deb','--root-owner-group','--build',str(stage),str(output/f'panasonic-on-ubuntu_{VERSION}_all.deb')],check=True)

if __name__ == '__main__':
    build()
