import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1]/'tools'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseTests(unittest.TestCase):
    def test_process_documents_and_private_artifacts_are_excluded(self):
        release = load('check_release')
        for path in ('docs/next-stage.zh-CN.md', 'docs/release-0.20.1.zh-CN.md',
                     'work/password.txt', 'vendor/backend/target/binary', 'src/model.onnx',
                     'tools/session.log', 'profiles/machine.json', 'kernel/pou_firmware.ko',
                     'src/model.bin', 'src/model.safetensors'):
            self.assertFalse(release.allowed(path), path)
        self.assertTrue(release.allowed('docs/usage.zh-CN.md'))
        self.assertTrue(release.allowed('kernel/COPYING'))

    def test_license_collection_includes_text_and_excludes_unused_dependencies(self):
        collector = load('collect_rust_licenses')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dep = root/'private-cache'/'dependency'
            dep.mkdir(parents=True)
            (dep/'LICENSE-MIT').write_text('Copyright Example\nPermission notice')
            (dep/'NOTICE').write_text('Required attribution')
            package = dict(name='dependency', version='1.0.0', manifest_path=str(dep/'Cargo.toml'),
                           license='MIT', repository='https://example.org/dependency')
            metadata = dict(packages=[package], workspace_members=[])
            destination = root/'notices'
            with patch.object(collector.subprocess, 'check_output',
                              side_effect=[json.dumps(metadata), 'dependency v1.0.0\n']):
                collector.collect(root, 'example', destination)
            self.assertEqual(len(list((destination/'dependency-1.0.0').iterdir())), 2)
            sources = (destination/'SOURCES.txt').read_text()
            self.assertIn('https://example.org/dependency', sources)
            self.assertNotIn(str(root), sources)

    def test_missing_license_text_stops_packaging(self):
        collector = load('collect_rust_licenses')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = dict(packages=[dict(id='dep', name='dependency', version='1.0.0',
                                           manifest_path=str(root/'Cargo.toml'))], workspace_members=[])
            with patch.object(collector.subprocess, 'check_output',
                              side_effect=[json.dumps(metadata), 'dependency v1.0.0\n']):
                with self.assertRaisesRegex(RuntimeError, 'Missing license text'):
                    collector.collect(root, 'example', root/'notices')

    def test_path_remapping_preserves_existing_compiler_options(self):
        rust = load('rust_build')
        with patch.dict(rust.os.environ, {'RUSTFLAGS': '-C opt-level=2'}, clear=True), \
                patch.object(rust.Path, 'home', return_value=Path.cwd()/'example-home'):
            result = rust.environment(Path.cwd()/'source with spaces', 'test')
        flags = result['CARGO_ENCODED_RUSTFLAGS'].split('\x1f')
        self.assertEqual(flags[:2], ['-C', 'opt-level=2'])
        self.assertTrue(flags[-1].endswith('source with spaces=/build/test'))
        self.assertEqual(result['CARGO_BUILD_JOBS'], '2')
