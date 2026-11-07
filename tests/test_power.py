import errno
import unittest
import time
import tempfile
from pathlib import Path
from unittest.mock import patch
from panasonic_on_ubuntu import power
from panasonic_on_ubuntu.power import thermal_target, Controller


class ThermalGuardTests(unittest.TestCase):
    def test_cool_boost_starts_above_performance_and_unlocks_stock_burst(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rapl, pstate, firmware = (root / name for name in ('rapl','pstate','firmware'))
            paths = {rapl/'constraint_0_power_limit_uw':'20000000', rapl/'constraint_1_power_limit_uw':'44000000',
                     rapl/'enabled':'1', pstate/'no_turbo':'0', firmware/'fan_profile':'0',
                     firmware/'performance_lease_version':'1', firmware/'performance_lease':'0'}
            for path, initial in paths.items():
                path.parent.mkdir(exist_ok=True)
                path.write_text(initial)
            original_read = Path.read_text
            def read_text(path, *args, **kwargs):
                constants = {'/proc/cpuinfo':'i5-8250U', '/sys/class/dmi/id/product_name':'CFSV7-3L',
                             '/sys/class/dmi/id/bios_version':'V3.11L22',
                             '/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw':'44000000',
                             '/sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw':'44000000'}
                if path.as_posix() in constants:
                    return constants[path.as_posix()]
                return original_read(path,*args,**kwargs)
            with patch.object(power,'RAPL',rapl), patch.object(power,'PSTATE',pstate), \
                 patch.object(power,'FIRMWARE',firmware), patch.object(power,'RUNTIME',root), \
                 patch.object(Path,'read_text',read_text), patch.object(power,'temperature',return_value=49), \
                 patch.object(power.power_profiles,'get_active',return_value='balanced'), patch.object(power.power_profiles,'set_active'):
                controller = Controller()
                sample = {'received':time.monotonic()}
                controller.apply('boost',sample)
                self.assertEqual(controller.target,25)
                self.assertEqual(power.read(rapl/'constraint_1_power_limit_uw'),'25000000')
                controller.tick(sample)
                self.assertEqual(power.read(firmware/'performance_lease'),'1')
                self.assertEqual(power.read(rapl/'constraint_1_power_limit_uw'),'44000000')
                controller.restore()
                self.assertEqual(power.read(rapl/'constraint_0_power_limit_uw'),'20000000')
                self.assertEqual(power.read(firmware/'performance_lease'),'0')

    def test_boost_short_burst_honors_external_and_stale_limits(self):
        controller = Controller()
        controller.mode = 'boost'
        controller.ceiling = controller.target = 25
        controller.short_ceiling = 44
        short = power.RAPL / 'constraint_1_power_limit_uw'
        controller.baseline = {'files': {str(short): '44000000'}}
        controller.last[str(short)] = '44000000'
        values = {'constraint_0_power_limit_uw': '25000000', short.name: '30000000',
                  'performance_lease': '1', 'lease_reason': 'active'}
        with patch.object(power,'temperature',return_value=50), patch.object(power,'atomic'), \
             patch.object(power,'read',side_effect=lambda p, *args: values[p.name]), \
             patch.object(controller,'write') as writer:
            controller.tick({'received': time.monotonic()})
            self.assertEqual(controller.short_ceiling, 30)
            self.assertEqual(controller.baseline['files'][str(short)], '30000000')
            self.assertNotIn((short, 44000000), [call.args for call in writer.call_args_list])
            controller.tick({'received': time.monotonic()-15})
            self.assertIn((short, 6000000), [call.args for call in writer.call_args_list])

    def test_sleep_busy_does_not_abandon_selected_boost_policy(self):
        controller=Controller();controller.mode='boost';controller.target=12;controller.ceiling=25;controller.baseline={}
        values={'constraint_0_power_limit_uw':'12000000','performance_lease':'0','lease_reason':'sleep'}
        def write(path,value):
            if path.name=='performance_lease':raise OSError(errno.EBUSY,'PM transition')
        with patch.object(power,'temperature',return_value=40),patch.object(power,'read',side_effect=lambda p:values.get(p.name)),patch.object(power,'atomic'),patch.object(controller,'write',side_effect=write):
            controller.tick({'received':time.monotonic()})
        self.assertEqual(controller.mode,'boost')
        self.assertIn('睡眠',controller.error)
        with patch.object(power,'temperature',return_value=40),patch.object(power,'read',side_effect=lambda p:values.get(p.name)),patch.object(power,'atomic'),patch.object(controller,'write') as writer:
            controller.tick({'received':time.monotonic()})
        self.assertIn((power.FIRMWARE/'performance_lease',1),[c.args for c in writer.call_args_list])
        self.assertEqual(controller.error,'')

    def test_kernel_temperature_race_cools_but_io_failure_propagates(self):
        for error in (errno.EAGAIN,errno.EIO):
            controller=Controller();controller.mode='boost';controller.target=12;controller.ceiling=25;controller.baseline={}
            values={'constraint_0_power_limit_uw':'12000000','performance_lease':'0','lease_reason':'released'}
            def write(path,value):
                if path.name=='performance_lease':raise OSError(error,'kernel rejection')
            with patch.object(power,'temperature',return_value=40),patch.object(power,'read',side_effect=lambda p:values.get(p.name)),patch.object(power,'atomic'),patch.object(controller,'write',side_effect=write):
                if error==errno.EIO:
                    with self.assertRaises(OSError):controller.tick({'received':time.monotonic()})
                else:
                    controller.tick({'received':time.monotonic()})
                    self.assertGreater(controller.cool_until,time.monotonic()+50)

    def test_boost_only_overrides_validated_stock_ceiling(self):
        self.assertEqual(power.boost_ceiling(20,25),25)
        self.assertEqual(power.boost_ceiling(4,25),4)
        self.assertEqual(power.boost_ceiling(20,18),18)
        self.assertEqual(power.boost_ceiling(30,44),25)
    def test_ec_lease_requires_fresh_sensor_and_cooling_hysteresis(self):
        self.assertFalse(power.permit_ec_lease(None,True,True,False))
        self.assertFalse(power.permit_ec_lease(40,False,True,False))
        self.assertFalse(power.permit_ec_lease(40,True,True,True))
        self.assertFalse(power.permit_ec_lease(92,True,True,False))
        self.assertFalse(power.permit_ec_lease(87,True,False,False))
        self.assertTrue(power.permit_ec_lease(90,True,True,False))
        self.assertTrue(power.permit_ec_lease(85,True,False,False))

    def test_stale_sampler_reduces_rapl_and_releases_ec(self):
        controller=Controller();controller.mode='boost';controller.target=20;controller.ceiling=20;controller.baseline={}
        values={'constraint_0_power_limit_uw':'20000000','performance_lease':'1','lease_reason':'active'}
        with patch.object(power,'temperature',return_value=50),patch.object(power,'read',side_effect=lambda p:values.get(p.name)),patch.object(power,'atomic'),patch.object(controller,'write') as writer:
            controller.tick({'received':time.monotonic()-20})
        calls=[c.args for c in writer.call_args_list]
        self.assertIn((power.RAPL/'constraint_0_power_limit_uw',6000000),calls)
        self.assertIn((power.FIRMWARE/'performance_lease',0),calls)
        self.assertNotIn((power.FIRMWARE/'performance_lease',1),calls)

    def test_kernel_thermal_trip_prevents_immediate_reenable(self):
        controller=Controller();controller.mode='boost';controller.target=12;controller.ceiling=20;controller.baseline={}
        values={'constraint_0_power_limit_uw':'12000000','performance_lease':'0','lease_reason':'temperature'}
        with patch.object(power,'temperature',return_value=50),patch.object(power,'read',side_effect=lambda p:values.get(p.name)),patch.object(power,'atomic'),patch.object(controller,'write') as writer:
            controller.tick({'received':time.monotonic()})
        self.assertGreater(controller.cool_until,time.monotonic()+50)
        self.assertTrue(all(c.args!=(power.FIRMWARE/'performance_lease',1) for c in writer.call_args_list))

    def test_write_intent_is_recoverable_before_hardware_changes(self):
        controller=Controller()
        controller.baseline={'files':{'test-limit':'20000000'},'profile':'balanced'}
        events=[]
        class Target:
            name='test-limit'
            def __str__(self):return self.name
            def write_text(self,value):events.append(('write',value))
        with patch.object(power,'atomic',side_effect=lambda _,v,*args:events.append(('saved',dict(v['last'])))),patch.object(power,'read',return_value='6000000'):
            controller.write(Target(),6000000)
        self.assertEqual(events,[('saved',{'test-limit':'6000000'}),('write','6000000\n')])

    def test_restore_leaves_external_power_profile_unchanged(self):
        controller=Controller()
        controller.baseline={'files':{},'profile':'balanced','last_profile':'performance'}
        with tempfile.TemporaryDirectory() as directory,patch.object(power,'RUNTIME',Path(directory)),patch.object(power,'read',return_value=None),patch.object(power.power_profiles,'get_active',return_value='power-saver'),patch.object(power.power_profiles,'set_active') as command:
            controller.restore()
        command.assert_not_called()
        self.assertIsNone(controller.baseline)

    def test_restore_returns_owned_power_profile(self):
        controller=Controller()
        controller.baseline={'files':{},'profile':'balanced','last_profile':'performance'}
        with tempfile.TemporaryDirectory() as directory,patch.object(power,'RUNTIME',Path(directory)),patch.object(power,'read',return_value=None),patch.object(power.power_profiles,'get_active',return_value='performance'),patch.object(power.power_profiles,'set_active') as command:
            controller.restore()
        command.assert_called_once_with('balanced')

    def test_bus_loss_releases_hardware_and_retains_restore_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            limit = root / 'constraint_0_power_limit_uw'
            lease = root / 'performance_lease'
            limit.write_text('12000000')
            lease.write_text('1')
            controller = Controller()
            controller.mode = 'boost'
            controller.baseline = {'files': {str(limit): '20000000'},
                                   'profile': 'performance', 'last_profile': 'balanced'}
            controller.last = {str(limit): '12000000'}
            with patch.object(power, 'RUNTIME', root), patch.object(power, 'FIRMWARE', root), \
                 patch.object(power.power_profiles, 'get_active', side_effect=power.power_profiles.ProfilesUnavailable('bus closed')):
                power.atomic(root / 'baseline.json', controller.baseline)
                with self.assertRaises(power.power_profiles.ProfilesUnavailable):
                    controller.restore()
                self.assertEqual(lease.read_text().strip(), '0')
                self.assertEqual(limit.read_text().strip(), '20000000')
                self.assertEqual(controller.mode, 'system')
                self.assertIsNotNone(controller.baseline)
                self.assertTrue((root / 'baseline.json').exists())
                with patch.object(controller, 'write') as writer:
                    controller.tick({'received': time.monotonic()})
                writer.assert_not_called()
            with patch.object(power, 'RUNTIME', root), patch.object(power, 'FIRMWARE', root), \
                 patch.object(power.power_profiles, 'get_active', return_value='balanced'), \
                 patch.object(power.power_profiles, 'set_active') as setter:
                controller.restore()
            setter.assert_called_once_with('performance')
            self.assertIsNone(controller.baseline)
            self.assertFalse((root / 'baseline.json').exists())

    def test_invalid_counter_samples_are_not_displayed_as_measurements(self):
        values={'PkgWatt':12,'Bzy_MHz':2400,'Avg_MHz':1200,'Busy%':50,'PkgTmp':60}
        self.assertTrue(power.valid_counters(values))
        for invalid in (float('nan'),float('inf'),-1,9999):
            self.assertFalse(power.valid_counters({**values,'PkgWatt':invalid}))

    def test_emergency_never_raises_external_four_watt_limit(self):
        for temperature in (None,84,88,91,95):
            self.assertLessEqual(thermal_target(4,4,temperature),4)

    def test_adaptive_never_disables_ec_limits(self):
        controller=Controller()
        controller.mode='adaptive';controller.target=12;controller.ceiling=20;controller.baseline={}
        with patch.object(power,'temperature',return_value=80),patch.object(power,'read',return_value='12000000'),patch.object(power,'atomic'),patch.object(controller,'write') as writer:
            controller.tick({'received':time.monotonic()})
            self.assertEqual(controller.target,13)
            self.assertTrue(all(call.args[0]!=power.FIRMWARE/'power_limits_enabled' for call in writer.call_args_list))

    def test_unstable_ec_mode_rejected_before_hardware_writes(self):
        controller=Controller()
        with self.assertRaisesRegex(RuntimeError,'暂停'):
            controller.apply('thermal')
        self.assertIsNone(controller.baseline)

    def test_sensor_loss_is_low_power(self):
        self.assertEqual(thermal_target(25, 25, None), 6)

    def test_emergency_temperature_is_low_power(self):
        self.assertEqual(thermal_target(25, 25, 94), 6)

    def test_cooling_recovers_gradually_and_honors_external_ceiling(self):
        self.assertEqual(thermal_target(12, 20, 83), 13)
        self.assertEqual(thermal_target(20, 20, 83), 20)
        self.assertEqual(thermal_target(18, 12, 83), 12)

    def test_hysteresis_avoids_chatter(self):
        self.assertEqual(thermal_target(19, 25, 88), 19)
        self.assertEqual(thermal_target(19, 25, 91), 17)
        self.assertEqual(thermal_target(6, 25, 93), 6)
