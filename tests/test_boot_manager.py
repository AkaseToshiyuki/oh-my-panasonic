import dataclasses,json,os,struct,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from panasonic_on_ubuntu import boot_manager as m,power
from panasonic_on_ubuntu.boot_unlock import Backup,BootState,Phase

def original():
    data=bytearray(961);data[0x30]=data[0x227]=1
    for offset in (0x5b,0x65,0x6f):struct.pack_into('<II',data,offset,28000,55000)
    return bytes(data)

class BootManagerTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='posix','POSIX file modes')
    def test_status_is_readable_with_restrictive_systemd_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'runtime'
            old=os.umask(0o077)
            try:
                with patch.object(m,'RUNTIME',root):
                    m.record(root/'boot-power-envelope.json',{'private':True})
                    m.publish('active')
                self.assertEqual(root.stat().st_mode&0o777,0o755)
                self.assertEqual((root/'boot-unlock.json').stat().st_mode&0o777,0o644)
                self.assertEqual((root/'boot-power-envelope.json').stat().st_mode&0o777,0o600)
            finally:os.umask(old)
    def make_mmio(self,root,pl1='0'):
        values={'enabled':'0','constraint_0_power_limit_uw':pl1,'constraint_1_power_limit_uw':'0',
            'constraint_0_time_window_us':'976','constraint_1_time_window_us':'976'}
        for name,value in values.items():(root/name).write_text(value)
        return values

    def test_both_observed_initial_states_become_safe_stock_baseline(self):
        for pl1 in ('0','35000000'):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);self.make_mmio(root,pl1);result=m.normalize_mmio(root)
                self.assertTrue(result['changed']);self.assertEqual(result['after']['constraint_0_power_limit_uw'],'28000000')
                self.assertEqual((root/'constraint_1_time_window_us').read_text().strip(),'1952')
                self.assertFalse(m.normalize_mmio(root)['changed'])

    def test_unexpected_limits_or_windows_are_not_overwritten(self):
        for name,value in [('constraint_0_power_limit_uw','25000000'),('enabled','1'),('constraint_0_time_window_us','1000')]:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);self.make_mmio(root);(root/name).write_text(value)
                before={p.name:p.read_bytes() for p in root.iterdir()}
                with self.assertRaises(RuntimeError):m.normalize_mmio(root)
                self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir()})

    def test_write_failure_restores_disabled_limits_and_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);before=self.make_mmio(root);original_write=Path.write_text;failed=False
            def write(path,data,*args,**kwargs):
                nonlocal failed
                if path.name=='enabled' and data.strip()=='1' and not failed:
                    failed=True;raise OSError('injected hardware failure')
                return original_write(path,data,*args,**kwargs)
            with patch.object(Path,'write_text',write),self.assertRaises(OSError):m.normalize_mmio(root)
            self.assertEqual(before,{p.name:p.read_text().strip() for p in root.iterdir()})

    def test_foreign_bootnext_blocks_restore_request_before_mutation(self):
        state=BootState(Phase.ACTIVE,1,0,'CF-SR3SKAKS','V1.00L27')
        with patch.object(m,'identity'),patch.object(m,'mount_device'),patch.object(m,'state_checked',return_value=(BootState(Phase.ACTIVE,1,0,'CF-SR3SKAKS','V1.00L27'),None)), \
             patch.object(m,'config',return_value={'entry':7}),patch.object(m,'variable',return_value=struct.pack('<H',9)), \
             patch.object(Path,'read_bytes',return_value=state.encode()), \
             patch.object(m,'save_phase') as save,patch.object(m,'run') as run:
            with self.assertRaises(RuntimeError):m.disable()
            save.assert_not_called();run.assert_not_called()

    def test_health_ack_requires_fresh_services_and_85_degree_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'enabled').touch()
            backup=Backup('CF-SR3SKAKS','V1.00L27',original())
            state=BootState(Phase.BOOT_PENDING,2,backup.checksum,backup.sku,backup.bios)
            (root/'state.bin').write_bytes(state.encode())
            (root/'boot-power-envelope.json').write_text(json.dumps({'boot_id':'test','generation':2}))
            care={'timestamp':time.time(),'error':''};(root/'battery-care.json').write_text(json.dumps(care))
            status={'timestamp':time.time(),'sample':{'received':time.monotonic(),'temperature':40},
                'presets':{'adaptive':{'cutoff_c':85}},'error':'','fan':{'error':''}}
            with patch.object(m,'STORE',root),patch.object(m,'RUNTIME',root),patch.object(m,'DIRECTORY',root), \
                 patch.object(m,'state_checked',return_value=(state,backup)),patch.object(m,'boot_id',return_value='test'), \
                 patch.object(m,'msr_limit',return_value=0x804281b800dd8118),patch.object(m,'run'),patch.object(m,'publish'),patch.object(m,'save_phase') as save:
                for invalid in ({'timestamp':0},{'error':'failure'},{'presets':{'adaptive':{'cutoff_c':95}}},
                                {'sample':{'received':0,'temperature':40}},{'fan':{'error':'failure'}}):
                    (root/'power.json').write_text(json.dumps(status|invalid))
                    with self.assertRaises(RuntimeError):m.confirm()
                    save.assert_not_called()
                (root/'power.json').write_text(json.dumps(status));m.confirm();save.assert_called_once_with(state,Phase.ACTIVE)

    def test_booting_original_ubuntu_entry_never_grants_35w(self):
        for phase in (Phase.ACTIVE,Phase.BOOT_PENDING):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'enabled').touch();self.make_mmio(root)
                backup=Backup('CF-SR3SKAKS','V1.00L27',original())
                state=BootState(phase,2,backup.checksum,backup.sku,backup.bios)
                with patch.object(m,'STORE',root),patch.object(m,'RUNTIME',root),patch.object(m,'RAPL',root), \
                     patch.object(m,'state_checked',return_value=(state,backup)),patch.object(m,'config',return_value={'entry':7}), \
                     patch.object(m,'variable',return_value=struct.pack('<H',0)),patch.object(m,'msr_limit',return_value=0x804281b800dd8118):
                    with self.assertRaises(RuntimeError):m.initialize()
                    self.assertEqual((root/'constraint_0_power_limit_uw').read_text().strip(),'28000000')
                    self.assertFalse((root/'boot-power-envelope.json').exists())

    @unittest.skipUnless(os.name=='posix','root-owned runtime records require POSIX')
    def test_verified_envelope_only_raises_exact_stock_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);envelope={'boot_id':'boot','sku':'CF-SR3SKAKS','bios':'V1.00L27','generation':1,
                'pl1_w':35,'pl2_w':55,'baseline_pl1_w':28,'baseline_pl2_w':55}
            path=root/'boot-power-envelope.json';path.write_text(json.dumps(envelope));path.chmod(0o600)
            values={'boot_id':'boot','product_sku':'CF-SR3SKAKS','bios_version':'V1.00L27','product_name':'CFSR3-1'}
            # The test need not run as root; ownership is modeled explicitly.
            actual=path.stat();fake=type('Stat',(),{'st_uid':0,'st_mode':actual.st_mode})()
            with patch.object(power,'RUNTIME',root),patch.object(power,'read',side_effect=lambda p:values.get(p.name)), \
                 patch.object(Path,'stat',return_value=fake):
                self.assertEqual(power.boot_ceiling(28,55,{'pl1_w':35,'pl2_w':55}),35)
                self.assertEqual(power.boot_ceiling(20,55,{'pl1_w':35,'pl2_w':55}),20)
                self.assertEqual(power.boot_ceiling(28,30,{'pl1_w':35,'pl2_w':55}),28)
                self.assertEqual(power.boot_ceiling(28,55,{'pl1_w':28,'pl2_w':55}),28)
                values['boot_id']='other';self.assertEqual(power.boot_ceiling(28,55,{'pl1_w':35,'pl2_w':55}),28)
                values['boot_id']='boot';fake.st_mode|=0o002
                self.assertEqual(power.boot_ceiling(28,55,{'pl1_w':35,'pl2_w':55}),28)

if __name__=='__main__':unittest.main()
