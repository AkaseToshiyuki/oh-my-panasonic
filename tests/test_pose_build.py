import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


def load_builder():
    tools = Path(__file__).resolve().parents[1]/'tools'
    with patch.object(sys, 'path', [str(tools), *sys.path]):
        spec = importlib.util.spec_from_file_location('pose_builder', tools/'build_pose_helper.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
    return builder


class PoseBuildTests(unittest.TestCase):
    def test_missing_argument_displays_usage_without_a_traceback(self):
        builder = load_builder()
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as result:
            builder.main([])
        self.assertEqual(result.exception.code, 2)
        self.assertIn('usage:', output.getvalue())
        self.assertNotIn('Traceback', output.getvalue())

    def test_missing_tpm_headers_stop_before_compilation(self):
        builder = load_builder()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(builder.shutil, 'which', return_value='/usr/bin/tool'), \
                patch.object(builder.sys, 'version_info', (3, 12)), \
                patch.object(builder.subprocess, 'check_output', return_value='rustc 1.93.1'), \
                patch.object(builder.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), \
                patch.object(builder, 'build') as compile_helper, contextlib.redirect_stderr(output):
            self.assertEqual(builder.main([directory]), 1)
        compile_helper.assert_not_called()
        self.assertIn('sudo apt install libtss2-dev pkg-config', output.getvalue())
        self.assertNotIn('Traceback', output.getvalue())

    def test_cargo_failure_exits_normally(self):
        builder = load_builder()
        output = io.StringIO()
        with patch.object(builder, 'check_dependencies'), \
                patch.object(builder, 'build', side_effect=subprocess.CalledProcessError(101, ['cargo', 'build'])), \
                contextlib.redirect_stderr(output):
            self.assertEqual(builder.main(['source']), 1)
        self.assertIn('status 101', output.getvalue())
        self.assertNotIn('Traceback', output.getvalue())

    def test_archive_failure_exits_normally(self):
        builder = load_builder()
        output = io.StringIO()
        with patch.object(builder, 'check_dependencies'), \
                patch.object(builder, 'build', side_effect=builder.tarfile.ReadError('invalid source archive')), \
                contextlib.redirect_stderr(output):
            self.assertEqual(builder.main(['source']), 1)
        self.assertIn('invalid source archive', output.getvalue())

    def test_cancelled_build_has_no_traceback(self):
        builder = load_builder()
        output = io.StringIO()
        with patch.object(builder, 'check_dependencies'), \
                patch.object(builder, 'build', side_effect=KeyboardInterrupt), contextlib.redirect_stderr(output):
            self.assertEqual(builder.main(['source']), 130)
        self.assertIn('cancelled', output.getvalue())

    def test_successful_build_keeps_source_path_and_result(self):
        builder = load_builder()
        with patch.object(builder, 'check_dependencies'), patch.object(builder, 'build') as compile_helper, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main(['source']), 0)
        compile_helper.assert_called_once_with(Path('source').resolve())
