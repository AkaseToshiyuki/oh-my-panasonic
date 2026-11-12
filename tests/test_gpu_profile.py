import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from panasonic_on_ubuntu import face_gpu as gpu


class ProfileTests(unittest.TestCase):
    def test_model_and_device_pair_select_the_correct_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'vendor').write_text('0x8086')
            with patch.object(gpu,'PCI',root),patch.object(Path,'is_file',return_value=True):
                for model,device in [('sv7','0x5917'),('sr3','0x46a8')]:
                    (root/'device').write_text(device)
                    with patch.object(gpu,'selection',return_value=(model,[])):
                        self.assertEqual(gpu.profile()['driver'],gpu.PROFILES[(model,device)])
                    with patch.object(gpu,'selection',return_value=('unknown',[])):
                        self.assertIsNone(gpu.profile())

    def test_new_hardware_requires_matching_local_graph_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'current').mkdir()
            selected={'model':'sr3','device':'0x46a8','driver':'/driver'}
            with patch.object(gpu,'profile',return_value=selected):
                self.assertIsNone(gpu.provisioned(root))
                (root/'current/backend.json').write_text(json.dumps(selected))
                self.assertEqual(gpu.provisioned(root),selected)
                (root/'current/backend.json').write_text('{}')
                self.assertIsNone(gpu.provisioned(root))
            (root/'current/backend.json').unlink()
            with patch.object(gpu,'profile',return_value={**selected,'model':'sv7'}):
                self.assertEqual(gpu.provisioned(root)['model'],'sv7')
