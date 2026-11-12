import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from panasonic_on_ubuntu import power


class SR3PowerTests(unittest.TestCase):
    def test_sku_and_bios_must_both_match(self):
        values={'product_name':'CFSR3-1','bios_version':'V1.00L12','product_sku':'CF-SR3SKAKS'}
        with patch.object(power,'read',side_effect=lambda p:values.get(p.name)):
            self.assertEqual(power.model_presets()['performance']['pl1'],28)
            self.assertNotIn('boost',power.model_presets())
            values['bios_version']='V1.00L99'
            self.assertEqual(power.model_presets(),{})
            values['bios_version']='V1.00L12';values['product_sku']='different'
            self.assertEqual(power.model_presets(),{})

    def test_sr3_preset_respects_firmware_ceiling_and_restores_every_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);rapl=root/'rapl';pstate=root/'pstate';firmware=root/'firmware'
            initial={rapl/'enabled':'1',rapl/'constraint_0_power_limit_uw':'18000000',
                rapl/'constraint_1_power_limit_uw':'25000000',pstate/'no_turbo':'0',firmware/'fan_profile':'1'}
            for p,v in initial.items():p.parent.mkdir(exist_ok=True);p.write_text(v)
            with patch.object(power,'RAPL',rapl),patch.object(power,'PSTATE',pstate), \
                patch.object(power,'FIRMWARE',firmware),patch.object(power,'RUNTIME',root), \
                patch.object(power,'model_presets',return_value=power.SR3_PRESETS), \
                patch.object(power,'temperature',return_value=30), \
                patch.object(power.power_profiles,'get_active',side_effect=['balanced','performance']), \
                patch.object(power.power_profiles,'set_active'):
                controller=power.Controller();controller.apply('performance')
                self.assertEqual(controller.ceiling,18)
                self.assertEqual((rapl/'constraint_1_power_limit_uw').read_text().strip(),'25000000')
                self.assertFalse((firmware/'performance_lease').exists())
                controller.restore()
                for p,v in initial.items():self.assertEqual(p.read_text().strip(),v)
