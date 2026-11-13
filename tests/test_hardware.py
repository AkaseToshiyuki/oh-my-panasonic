import unittest
from pathlib import Path
from unittest.mock import patch, call
from panasonic_on_ubuntu import hardware


class HardwareTests(unittest.TestCase):
    def test_selection_and_narrow_battery_gate(self):
        sv7={'product_name':'CFSV7-3L','bios_version':'V3.11L22','product_sku':'CF-SV7HD9VS'}
        self.assertEqual(hardware.selection(sv7),('sv7',['pou_firmware','pou_battery']))
        self.assertEqual(hardware.selection({**sv7,'product_sku':'different'}),('sv7',['pou_firmware']))
        self.assertEqual(hardware.selection({**sv7,'bios_version':'unknown'}),(None,[]))
        sr3={'product_name':'CFSR3-1','bios_version':'V1.00L12','product_sku':'CF-SR3SKAKS'}
        for bios in ('V1.00L12','V1.00L27'):
            sr3['bios_version']=bios
            self.assertEqual(hardware.selection(sr3),('sr3',['pou_sr3_fan','pou_battery']))
            self.assertEqual(hardware.selection({**sr3,'product_sku':'different'}),(None,[]))
            self.assertEqual(hardware.selection({**sr3,'product_name':'different'}),(None,[]))
        for bios in ('V1.00L28','V1.00L27-test','',None):
            self.assertEqual(hardware.selection({**sr3,'bios_version':bios}),(None,[]))
        self.assertEqual(hardware.selection({}),(None,[]))

    def test_common_installer_selects_only_matching_backend(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            load=Path(directory)/'modules.conf'
            with patch.object(hardware,'LOAD',load),patch.object(hardware,'selection',return_value=('sr3',['pou_sr3_fan','pou_battery'])),patch.object(hardware,'registered',return_value=False),patch.object(hardware,'unload'),patch.object(hardware,'run') as run:
                hardware.install()
            self.assertEqual(load.read_text(),'pou_sr3_fan\npou_battery\n')
            self.assertIn(call('modprobe','pou_sr3_fan'),run.call_args_list)
            self.assertNotIn(call('modprobe','pou_firmware'),run.call_args_list)
            self.assertIn(call('modprobe','pou_battery'),run.call_args_list)
            self.assertTrue(any(c.args[:4]==('nice','-n','15','dkms') and c.args[-2:]==('-j','1') for c in run.call_args_list))

    def test_build_failure_does_not_unload_working_backend(self):
        with patch.object(hardware,'selection',return_value=('sr3',['pou_sr3_fan'])),patch.object(hardware,'registered',return_value=True),patch.object(hardware,'run',side_effect=RuntimeError('build failed')),patch.object(hardware,'unload') as unload:
            with self.assertRaises(RuntimeError):hardware.install()
            unload.assert_not_called()

    def test_unknown_model_removes_stale_autoload(self):
        with patch.object(hardware,'selection',return_value=(None,[])),patch.object(hardware,'remove') as remove,patch.object(hardware,'run') as run:
            hardware.install()
            remove.assert_called_once_with()
            run.assert_not_called()

    def test_loaded_module_can_be_removed_before_dkms_install(self):
        with patch.object(Path,'exists',return_value=True),patch.object(hardware,'run') as run:
            hardware.unload()
        self.assertEqual(run.call_args_list,[call('rmmod','pou_battery'),call('rmmod','pou_firmware'),call('rmmod','pou_sr3_fan')])
