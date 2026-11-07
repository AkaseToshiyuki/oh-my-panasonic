import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class MetadataTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'POSIX permissions require Linux')
    def test_public_package_permissions_do_not_inherit_private_build_umask(self):
        spec = importlib.util.spec_from_file_location('deb_metadata', REPO/'tools/deb_metadata.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'DEBIAN').mkdir(mode=0o700)
            (root/'DEBIAN/control').write_text('Package: test\nVersion: 1\n')
            binary = root/'usr/bin/example'
            binary.parent.mkdir(parents=True)
            (root/'usr').chmod(0o700)
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o700)
            document = root/'usr/NOTICE'
            document.write_text('Required notice')
            document.chmod(0o600)
            module.finalize(root)
            for path in (root, root/'usr', binary.parent, binary):
                self.assertEqual(path.stat().st_mode & 0o777, 0o755)
            for path in (document, root/'DEBIAN/control', root/'DEBIAN/md5sums'):
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_installed_files_have_correct_checksum_and_size(self):
        spec = importlib.util.spec_from_file_location('deb_metadata', REPO/'tools/deb_metadata.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'DEBIAN').mkdir()
            (root/'DEBIAN/control').write_text('Package: test\nInstalled-Size: 999\nVersion: 1\n')
            payload = root/'usr/bin/example'
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b'a' * 1500)
            module.finalize(root)
            expected = hashlib.md5(payload.read_bytes(), usedforsecurity=False).hexdigest()
            self.assertEqual((root/'DEBIAN/md5sums').read_text(), f'{expected}  usr/bin/example\n')
            self.assertIn('Installed-Size: 2\n', (root/'DEBIAN/control').read_text())
            self.assertNotIn('999', (root/'DEBIAN/control').read_text())
            payload.write_bytes(b'tampered')
            self.assertNotEqual(hashlib.md5(payload.read_bytes(), usedforsecurity=False).hexdigest(), expected)


@unittest.skipUnless(os.name == 'posix', 'maintainer scripts require Linux')
class LifecycleTests(unittest.TestCase):
    def commands(self, script, action):
        # All external commands are replaced with recording shims. The real
        # script branches run without access to service control or deletion.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root/'commands'
            for name in ('rm', 'rmdir', 'systemctl', 'udevadm'):
                path = root/name
                path.write_text('#!/usr/bin/python3\nimport json,os,sys\n'
                                'with open(os.environ["COMMAND_LOG"],"a") as f:\n'
                                ' f.write(json.dumps([os.path.basename(sys.argv[0]),*sys.argv[1:]])+"\\n")\n')
                path.chmod(0o755)
            subprocess.run(['/bin/sh', str(REPO/'packaging'/script), action, '0.17.4'],
                           env={**os.environ, 'PATH': str(root), 'COMMAND_LOG': str(output)}, check=True)
            return [json.loads(line) for line in output.read_text().splitlines()] if output.exists() else []

    def test_gpu_upgrade_preserves_enabled_setting(self):
        self.assertEqual(self.commands('face-gpu-prerm', 'upgrade'), [])

    def test_gpu_removal_disables_acceleration(self):
        commands = self.commands('face-gpu-prerm', 'remove')
        self.assertEqual(commands, [['rm', '-f', '--', '/var/cache/pou-face-gpu/enabled']])

    def test_normal_upgrade_and_removal_preserve_preferences(self):
        for action in ('upgrade', 'remove'):
            with self.subTest(action=action):
                self.assertFalse(any(c[0] == 'rm' for c in self.commands('postrm', action)))

    def test_purge_clears_opt_ins_but_never_facelock_templates(self):
        commands = self.commands('postrm', 'purge')
        deleted = [p for c in commands if c[0] == 'rm' for p in c[1:] if p.startswith('/')]
        for name in ('face-unlock', 'face-authorize', 'supplemented'):
            self.assertIn('/var/lib/panasonic-on-ubuntu/' + name, deleted)
        self.assertIn('/etc/panasonic-on-ubuntu/fan-control.json', deleted)
        self.assertFalse(any(p.startswith('/var/lib/facelock') for p in deleted))


@unittest.skipUnless(os.name == 'posix', 'live validators require Linux')
class ValidationEntryTests(unittest.TestCase):
    def test_hardware_checks_default_to_help(self):
        for name in ('validate_power.py', 'validate_optical.py', 'validate_desktop.py',
                     'validate_battery_hardware.py', 'validate_battery_policy.py', 'validate_ssd_power.py'):
            with self.subTest(tool=name):
                result = subprocess.run(['/usr/bin/python3', str(REPO/'tools'/name)],
                                        env={**os.environ, 'PYTHONPATH': str(REPO/'src')},
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--run', result.stdout)
                self.assertNotIn('Traceback', result.stderr)

    def test_expected_failure_has_no_uncaught_exception(self):
        result = subprocess.run(['/usr/bin/python3', '-c',
                                 'from validation_cli import checked; '
                                 'checked(lambda: (_ for _ in ()).throw(RuntimeError("precondition")))'],
                                env={**os.environ, 'PYTHONPATH': str(REPO/'tools')}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('VALIDATION_FAILED: precondition', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
