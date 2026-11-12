import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from panasonic_on_ubuntu import power


class SR3PowerTests(unittest.TestCase):
    def test_83_degree_target_and_85_degree_cutoff(self):
        for temp,expected in [(78,28),(81,27),(83,25),(84,25),(85,6),(None,6)]:
            self.assertEqual(power.thermal_target(27,28,temp,83,85),expected)
        self.assertEqual(power.thermal_target(25,25,85),25)

    def test_adaptive_limits_both_windows_and_restores_original_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);rapl=root/'rapl';pstate=root/'pstate';firmware=root/'firmware'
            initial={rapl/'enabled':'1',rapl/'constraint_0_power_limit_uw':'28000000',
                rapl/'constraint_1_power_limit_uw':'55000000',pstate/'no_turbo':'0',firmware/'fan_profile':'1'}
            for p,v in initial.items():p.parent.mkdir(exist_ok=True);p.write_text(v)
            with patch.object(power,'RAPL',rapl),patch.object(power,'PSTATE',pstate), \
                patch.object(power,'FIRMWARE',firmware),patch.object(power,'RUNTIME',root), \
                patch.object(power,'model_presets',return_value=power.SR3_PRESETS), \
                patch.object(power,'temperature',return_value=30) as temp, \
                patch.object(power.power_profiles,'get_active',side_effect=['balanced','performance']), \
                patch.object(power.power_profiles,'set_active'):
                controller=power.Controller();controller.apply('adaptive')
                self.assertEqual(controller.temperature_target,83)
                self.assertEqual(controller.temperature_cutoff,85)
                self.assertEqual(controller.short_ceiling,55)
                controller.tick({'received':time.monotonic()})
                self.assertEqual((rapl/'constraint_1_power_limit_uw').read_text().strip(),'55000000')
                temp.return_value=85
                controller.tick({'received':time.monotonic()})
                for i in (0,1):self.assertEqual((rapl/f'constraint_{i}_power_limit_uw').read_text().strip(),'6000000')
                temp.return_value=30
                controller.tick({'received':time.monotonic()-20})
                self.assertEqual(controller.target,6)
                self.assertFalse((firmware/'performance_lease').exists())
                controller.restore()
                for p,v in initial.items():self.assertEqual(p.read_text().strip(),v)

    def test_adaptive_respects_external_short_limit(self):
        c=power.Controller();c.mode='adaptive';c.ceiling=c.target=28;c.short_ceiling=55
        c.guard_short_burst=True;c.temperature_target=83;c.temperature_cutoff=85
        path=power.RAPL/'constraint_1_power_limit_uw'
        c.baseline={'files':{str(path):'55000000'}};c.last[str(path)]='55000000'
        values={'constraint_0_power_limit_uw':'28000000','constraint_1_power_limit_uw':'20000000'}
        with patch.object(power,'temperature',return_value=30),patch.object(power,'atomic'), \
             patch.object(power,'read',side_effect=lambda p,*a:values[p.name]),patch.object(c,'write') as write:
            c.tick({'received':time.monotonic()})
        self.assertEqual(c.short_ceiling,20)
        self.assertEqual(c.baseline['files'][str(path)],'20000000')
        self.assertNotIn((path,55000000),[call.args for call in write.call_args_list])

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
