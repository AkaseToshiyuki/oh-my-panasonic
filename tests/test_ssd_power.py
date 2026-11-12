import copy
import struct
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from panasonic_on_ubuntu import ssd_power as ssd, legacy_io


def identify_data():
    raw=bytearray(4096)
    raw[24:64]=b'ZHITAI TiPlus5000 1TB'.ljust(40)
    raw[64:72]=b'ZTA10666'
    raw[263]=4
    for index,power in enumerate((650,580,360,7460,7260)):
        struct.pack_into('<H',raw,2048+index*32,power)
        raw[2051+index*32]=3 if index>2 else 0
    return bytes(raw)


class FakeDevice:
    path='/dev/nvme0'
    def __init__(self):
        self.value=0;self.idle=False;self.failure=False
    def read(self,wake=False):
        value=self.value if wake or not self.idle else 4
        return dict(ssd.identify(identify_data()),device=self.path,value=value,selected=value&31,
                    apst_enabled=True,apst_hash='same')
    def write(self,value,fingerprint):
        if self.failure:raise RuntimeError('NVMe unavailable')
        self.value=value
        return self.read(wake=True)


class SSDTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.device=FakeDevice()
        for name,value in [('ROOT',self.root),('CONFIG',self.root/'config.json'),('BASELINE',self.root/'baseline.json')]:
            p=patch.object(ssd,name,value);p.start();self.addCleanup(p.stop)
        self.controller=ssd.Controller()
        p=patch.object(ssd,'Device',return_value=self.device);p.start();self.addCleanup(p.stop)
        p=patch.object(ssd,'system_controller',return_value=self.device.path);p.start();self.addCleanup(p.stop)
    def tick(self,mode='balanced'):
        self.controller.next_check=0;self.controller.tick(mode);return ssd.status()
    def test_presets_set_operational_states_and_system_restores(self):
        for mode,selected in [('balanced',1),('eco',2),('performance',0),('adaptive',0),('boost',0)]:
            self.assertEqual(self.tick(mode)['selected'],selected)
        self.tick('system')
        self.assertEqual(self.device.value,0);self.assertFalse(ssd.BASELINE.exists())
    def test_idle_is_not_treated_as_selected_operational_state(self):
        self.tick('eco');self.device.idle=True
        value=self.tick('eco')
        self.assertEqual(value['current_state'],4)
        self.assertEqual(value['selected'],2)
        self.assertEqual(value['maximum_w'],3.6)
        ssd.release(self.device);self.assertEqual(self.device.value,0)
    def test_external_stricter_limit_is_preserved(self):
        self.device.value=2
        self.assertEqual(self.tick('performance')['selected'],2)
        ssd.release(self.device);self.assertEqual(self.device.value,2)
    def test_external_change_is_not_fought(self):
        self.tick('eco');self.device.value=1
        result=self.tick('eco')
        self.assertIn('其他程序',result['error'])
        self.assertEqual(self.tick('eco')['selected'],1)
        self.assertFalse(ssd.BASELINE.exists())
    def test_failed_restore_keeps_recovery_snapshot(self):
        self.tick();self.device.failure=True
        with self.assertRaises(RuntimeError):ssd.release(self.device)
        self.assertTrue(ssd.BASELINE.exists())
        self.device.failure=False;ssd.release(self.device)
        self.assertFalse(ssd.BASELINE.exists())
    def test_manual_mode_has_priority_over_cpu_mode(self):
        ssd.save(ssd.CONFIG,{'mode':'save','request_id':'test'})
        self.assertEqual(self.tick('performance')['selected'],2)
        self.assertEqual(ssd.status()['request_id'],'test')


class DriverTests(unittest.TestCase):
    def test_static_queries_cached_but_mutation_and_identity_force_refresh(self):
        device=ssd.Device('/dev/nvme0')
        def command(op,field,value=0,length=0):
            return (0,identify_data() if op==6 else bytes(256) if field==12 else b'')
        with patch.object(device,'identity_stamp',return_value='first') as identity, \
             patch.object(device,'_command',side_effect=command) as io, \
             patch.object(device,'active_value',return_value=0), \
             patch.object(ssd.time,'monotonic',return_value=1):
            device.read();device.read()
            self.assertEqual(sum(c.args==(6,1) for c in io.call_args_list),1)
            device.read(wake=True)
            self.assertEqual(sum(c.args==(6,1) for c in io.call_args_list),2)
            identity.return_value='replacement'
            device.read()
            self.assertEqual(sum(c.args==(6,1) for c in io.call_args_list),3)

    def test_silvermake_table_uses_100_microwatt_units(self):
        raw=bytearray(identify_data())
        raw[24:64]=b'SilverMake 2268XT2 B58R 1TB'.ljust(40)
        raw[64:72]=b'X0430M00'
        for index,power in enumerate((45000,24000,6000)):
            struct.pack_into('<H',raw,2048+32*index,power)
            raw[2051+32*index]=1
        self.assertEqual([round(p['maximum_w'],2) for p in ssd.identify(raw)['states']],[4.5,2.4,.6])
        raw[64]=ord('Y')
        with self.assertRaises(ValueError):ssd.identify(raw)

    def test_identity_and_table_are_allowlisted(self):
        value=ssd.identify(identify_data())
        self.assertEqual([p['maximum_w'] for p in value['states']],[6.5,5.8,3.6])
        for offset in (24,64,2051,2080):
            raw=bytearray(identify_data());raw[offset]^=1
            with self.assertRaises(ValueError):ssd.identify(raw)
    def test_only_volatile_power_feature_and_reads_can_reach_ioctl(self):
        d=ssd.Device('/dev/nvme0')
        with patch.object(ssd.os,'open') as opened:
            for args in ((9,12,0,256),(9,0x80000002,1,0),(9,2,3,0),(0x80,0,0,0)):
                with self.assertRaises(ValueError):d._command(*args)
            opened.assert_not_called()
        self.assertEqual(ssd.ADMIN.size,72)
    def test_apst_change_rolls_back_working_state(self):
        d=ssd.Device('/dev/nvme0');before=FakeDevice().read()
        after={**before,'value':2,'selected':2,'apst_hash':'changed'}
        with patch.object(d,'read',side_effect=[before,after]),patch.object(d,'_command') as command:
            with self.assertRaises(RuntimeError):d.write(2,before['fingerprint'])
            self.assertEqual(command.call_args_list[-1].kwargs,{'value':0})
    def test_wrong_device_identity_never_writes(self):
        d=ssd.Device('/dev/nvme0')
        with patch.object(d,'read',return_value=FakeDevice().read()),patch.object(d,'_command') as command:
            with self.assertRaises(RuntimeError):d.write(2,'other')
            command.assert_not_called()


class LegacyRemovalTests(unittest.TestCase):
    def test_remove_only_owned_limits_and_preserve_other_disks(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.json'
            device='/dev/nvme0n1';keys=legacy_io.PROPERTIES
            path.write_text(__import__('json').dumps({'device':device,'original':{k:None for k in keys},'last':{k:400 for k in keys}}))
            values={keys[0]:{device:400,'/dev/sdb':10},keys[1]:{device:250}}
            class Bus:
                def get(self):return copy.deepcopy(values)
                def set(self,updates):values.update(updates)
            with patch.object(legacy_io,'BASELINE',path):legacy_io.release(Bus())
            self.assertEqual(values,{keys[0]:{'/dev/sdb':10},keys[1]:{device:250}})
            self.assertFalse(path.exists())
