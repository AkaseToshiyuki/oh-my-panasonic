import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from panasonic_on_ubuntu import power


class SR3PowerTests(unittest.TestCase):
    def test_performance_to_highest_handoff_and_rollback(self):
        cases = [
            ('cool', 'performance', 79, 79, 0, None, 28),
            ('warm sensor', 'performance', 80, 55, 0, None, 12),
            ('warm sample', 'performance', 55, 80, 0, None, 12),
            ('stale', 'performance', 40, 40, 9, None, 12),
            ('future', 'performance', 40, 40, -10, None, 12),
            ('missing temperature', 'performance', 40, None, 0, None, 12),
            ('missing live sensor', 'performance', None, 40, 0, None, 6),
            ('thermal cutoff', 'performance', 85, 60, 0, None, 6),
            ('sample cutoff', 'performance', 60, 85, 0, None, 6),
            ('external cap', 'performance', 45, 45, 0, 18, 12),
            ('cold start', 'system', 40, 40, 0, None, 12),
            ('balanced', 'balanced', 40, 40, 0, None, 12),
        ]
        for name, source, live, sampled, age, external, start in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                long = root/'constraint_0_power_limit_uw'
                short = root/'constraint_1_power_limit_uw'
                initial = {long:'35000000', short:'55000000', root/'enabled':'1',
                           root/'fan_profile':'1', root/'no_turbo':'0'}
                for path, value in initial.items():
                    path.write_text(value)
                with patch.object(power,'RAPL',root), patch.object(power,'PSTATE',root), \
                     patch.object(power,'FIRMWARE',root), patch.object(power,'RUNTIME',root), \
                     patch.object(power,'model_presets',return_value=power.SR3_PRESETS), \
                     patch.object(power,'processor_limits',return_value={'pl1_w':35,'pl2_w':55}), \
                     patch.object(power,'temperature',return_value=40) as sensor, \
                     patch.object(power.power_profiles,'get_active',return_value='balanced') as profile, \
                     patch.object(power.power_profiles,'set_active') as set_profile:
                    ctl = power.Controller()
                    ctl.apply(source)
                    sensor.return_value = live
                    if source == 'performance':
                        profile.return_value = 'performance'
                    if external is not None:
                        long.write_text(str(external*1000000))
                    sample = {'received':time.monotonic()-age, 'temperature':sampled}
                    # Record raw writes, including writes by restore().
                    writes = []
                    write_text = Path.write_text
                    def track(path, data, *args, **kwargs):
                        if path == long:
                            writes.append(int(data))
                        return write_text(path, data, *args, **kwargs)
                    with patch.object(Path,'write_text',track):
                        ctl.apply('adaptive', sample)
                    self.assertEqual(ctl.target, start)
                    self.assertEqual(int(long.read_text()), start*1000000)
                    self.assertEqual(int(short.read_text()), start*1000000)
                    if name == 'cool':
                        self.assertEqual(writes, [28000000])
                        self.assertEqual(set_profile.call_count, 1)
                        ctl.tick(sample)
                        self.assertEqual(ctl.target, 28)
                        for expected in range(29,36):
                            ctl.tick(dict(sample, received=time.monotonic()))
                            self.assertEqual(ctl.target, expected)
                    if external is not None:
                        self.assertEqual(ctl.ceiling, external)
                    ctl.restore()
                    self.assertEqual(int(long.read_text()), (external or 35)*1000000)
                    self.assertEqual(int(short.read_text()), 55000000)
                    self.assertEqual((root/'fan_profile').read_text().strip(), '1')

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
                patch.object(power,'processor_limits',return_value={'pl1_w':28,'pl2_w':55}), \
                patch.object(power,'temperature',return_value=30) as temp, \
                patch.object(power.power_profiles,'get_active',side_effect=['balanced','performance']), \
                patch.object(power.power_profiles,'set_active'):
                controller=power.Controller();controller.apply('adaptive')
                self.assertEqual(controller.ceiling,28)
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
            for bios in ('V1.00L12','V1.00L27'):
                values['bios_version']=bios
                self.assertEqual(power.model_presets()['performance']['pl1'],28)
                self.assertEqual(power.model_presets()['adaptive']['pl1'],35)
                self.assertEqual(power.model_presets()['adaptive']['label'],'最高性能')
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

    def test_processor_limit_validation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(power,'PROCESSOR_RAPL',Path(directory)):
            root=Path(directory)
            self.assertEqual(power.processor_limits(),{})
            for value in ('0','-1000000','nan','inf','broken','201000000'):
                (root/'constraint_0_power_limit_uw').write_text(value)
                self.assertEqual(power.processor_limits(),{},value)
            (root/'constraint_0_power_limit_uw').write_text('35000000')
            (root/'constraint_1_power_limit_uw').write_text('55000000')
            self.assertEqual(power.processor_limits(),{'pl1_w':35,'pl2_w':55})

    def test_highest_preset_respects_both_domains_and_keeps_85_degree_guard(self):
        cases=[(40,55,28,55,28,55),(40,55,35,55,35,55),
               (40,55,40,55,35,55),(18,25,40,55,18,25),
               (40,55,40,25,25,25)]
        for mmio_long,mmio_short,msr_long,msr_short,long,short in cases:
            with self.subTest(limits=(mmio_long,mmio_short,msr_long,msr_short)),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);rapl=root/'rapl';pstate=root/'pstate';firmware=root/'firmware'
                initial={rapl/'enabled':'1',rapl/'constraint_0_power_limit_uw':str(mmio_long*1000000),
                    rapl/'constraint_1_power_limit_uw':str(mmio_short*1000000),
                    pstate/'no_turbo':'0',firmware/'fan_profile':'1'}
                for p,v in initial.items():p.parent.mkdir(exist_ok=True);p.write_text(v)
                with patch.object(power,'RAPL',rapl),patch.object(power,'PSTATE',pstate), \
                    patch.object(power,'FIRMWARE',firmware),patch.object(power,'RUNTIME',root), \
                    patch.object(power,'model_presets',return_value=power.SR3_PRESETS), \
                    patch.object(power,'processor_limits',return_value={'pl1_w':msr_long,'pl2_w':msr_short}), \
                    patch.object(power,'temperature',return_value=30) as temp, \
                    patch.object(power.power_profiles,'get_active',side_effect=['balanced','performance']), \
                    patch.object(power.power_profiles,'set_active'):
                    controller=power.Controller();controller.apply('adaptive')
                    for _ in range(30):controller.tick({'received':time.monotonic()})
                    self.assertEqual(controller.ceiling,long)
                    self.assertEqual(controller.target,long)
                    self.assertEqual(controller.short_ceiling,short)
                    self.assertEqual((rapl/'constraint_0_power_limit_uw').read_text().strip(),str(long*1000000))
                    self.assertEqual((rapl/'constraint_1_power_limit_uw').read_text().strip(),str(short*1000000))
                    temp.return_value=85
                    controller.tick({'received':time.monotonic()})
                    for i in (0,1):self.assertEqual((rapl/f'constraint_{i}_power_limit_uw').read_text().strip(),'6000000')
                    controller.restore()
                    for p,v in initial.items():self.assertEqual(p.read_text().strip(),v)
                    self.assertFalse((firmware/'performance_lease').exists())

    def test_missing_processor_limits_prevent_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);rapl=root/'rapl';pstate=root/'pstate';firmware=root/'firmware'
            initial={rapl/'enabled':'1',rapl/'constraint_0_power_limit_uw':'40000000',
                rapl/'constraint_1_power_limit_uw':'55000000',pstate/'no_turbo':'0',firmware/'fan_profile':'1'}
            for p,v in initial.items():p.parent.mkdir(exist_ok=True);p.write_text(v)
            with patch.object(power,'RAPL',rapl),patch.object(power,'PSTATE',pstate), \
                patch.object(power,'FIRMWARE',firmware),patch.object(power,'RUNTIME',root), \
                patch.object(power,'model_presets',return_value=power.SR3_PRESETS), \
                patch.object(power,'processor_limits',return_value={}), \
                patch.object(power,'temperature',return_value=30), \
                patch.object(power.power_profiles,'get_active',return_value='balanced'), \
                patch.object(power.power_profiles,'set_active') as profile:
                controller=power.Controller()
                with self.assertRaisesRegex(OSError,'处理器功率上限'):controller.apply('adaptive')
                self.assertIsNone(controller.baseline)
                self.assertEqual(controller.last,{})
                profile.assert_not_called()
                for p,v in initial.items():self.assertEqual(p.read_text().strip(),v)
