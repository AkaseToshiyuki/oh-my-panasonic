import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from panasonic_on_ubuntu import face


class GpuAuthenticationTests(unittest.TestCase):
    def test_cpu_when_gpu_is_unprovisioned(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(face, 'call') as cpu:
            root = Path(tmp)
            (root/'pou-face-gpu').touch()
            with patch.object(face, 'GPU_DIR', root), patch.object(face, 'GPU_ENABLED', root/'enabled'):
                face.authenticate('testuser')
            cpu.assert_called_once_with([face.BACKEND, 'auth', '-u', 'testuser'], timeout=12, check=False)

    def test_denial_is_final_for_this_face_attempt(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(face, 'call') as cpu:
            root = Path(tmp)
            (root/'pou-face-gpu').touch()
            (root/'enabled').touch()
            with patch.object(face, 'GPU_DIR', root), patch.object(face, 'GPU_ENABLED', root/'enabled'), \
                    patch('panasonic_on_ubuntu.face_gpu.provisioned',return_value={'driver':'/validated/driver.so'}), \
                    patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)) as gpu, \
                    patch('syslog.syslog'):
                self.assertEqual(face.authenticate('testuser').returncode, 1)
            cpu.assert_not_called()
            self.assertEqual(gpu.call_args.args[0], [root/'pou-face-gpu', 'auth', '-u', 'testuser'])
            self.assertEqual(gpu.call_args.kwargs['env']['LD_LIBRARY_PATH'], f'{root}:{root}/ov')
            self.assertEqual(gpu.call_args.kwargs['env']['OCL_ICD_VENDORS'],'/validated/driver.so')

    def test_incompatible_provisioning_uses_cpu_without_loading_gpu(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(face,'call') as cpu:
            root=Path(tmp);(root/'pou-face-gpu').touch();(root/'enabled').touch()
            with patch.object(face,'GPU_DIR',root),patch.object(face,'GPU_ENABLED',root/'enabled'), \
                 patch('panasonic_on_ubuntu.face_gpu.provisioned',return_value=None),patch.object(subprocess,'run') as gpu:
                face.authenticate('testuser')
            cpu.assert_called_once();gpu.assert_not_called()
