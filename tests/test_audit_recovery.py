"""Regression cases for ownership, failover and recovery across upgrades."""
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
import uuid

from panasonic_on_ubuntu import boot_manager as boot, care, face_privilege as pam
from panasonic_on_ubuntu import face_poses, hardware, peripherals, power, private_lock
from panasonic_on_ubuntu.boot_unlock import Backup, BootState, Phase
from test_boot_manager import original


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def mock(self, module, **fields):
        return self.stack.enter_context(patch.multiple(module, **fields))

    def test_fixed_modes_recover_after_temperature_or_sensor_fault(self):
        for presets in (power.PRESETS, power.SR3_PRESETS):
            for mode in ('eco','balanced','performance'):
                for bad in (None, 95):
                    with self.subTest(mode=mode, bad=bad, model=presets['performance']['pl1']), ExitStack() as ctx:
                        for path,value in {'enabled':1,'fan_profile':1,'no_turbo':0,'constraint_0_power_limit_uw':35000000,
                                           'constraint_1_power_limit_uw':55000000}.items():
                            (self.root/path).write_text(str(value))
                        ctx.enter_context(patch.multiple(power, RAPL=self.root,FIRMWARE=self.root,PSTATE=self.root,RUNTIME=self.root))
                        ctx.enter_context(patch.object(power,'model_presets',return_value=presets))
                        sensor=ctx.enter_context(patch.object(power,'temperature',return_value=42))
                        ctx.enter_context(patch.object(power.power_profiles,'get_active',return_value='balanced'))
                        ctx.enter_context(patch.object(power.power_profiles,'set_active'))
                        ctl=power.Controller()
                        ctl.apply(mode)
                        sensor.return_value=bad
                        ctl.tick(None)
                        self.assertEqual(ctl.target,6)
                        sensor.return_value=42
                        sample={'temperature':42,'received':time.monotonic()}
                        for _ in range(3): ctl.tick(sample)
                        self.assertEqual(ctl.target,6)
                        for _ in range(20): ctl.tick(dict(sample,received=time.monotonic()))
                        self.assertEqual(ctl.target,presets[mode]['pl1'])
                        (self.root/'constraint_0_power_limit_uw').write_text('4000000')
                        for _ in range(3): ctl.tick(dict(sample,received=time.monotonic()))
                        self.assertEqual(ctl.target,4)

    def test_transient_retry_and_external_limit_policy(self):
        retry=power.ProfileRetry()
        now=0
        for delay in (2,4,8,16,32,60,60):
            retry.failed(OSError("offline"),now)
            self.assertFalse(retry.profiles_unavailable)
            self.assertFalse(retry.due(now+delay-1))
            now+=delay
            self.assertTrue(retry.due(now))
        retry.failed(power.power_profiles.ProfilesUnavailable("startup"),now)
        self.assertTrue(retry.profiles_unavailable)
        retry.failed(RuntimeError("external limit disabled"),now)
        self.assertIsNone(retry.deadline)

    def test_confirm_wait_releases_transaction_lock_between_attempts(self):
        import contextlib
        held=[]
        @contextlib.contextmanager
        def lock(name):
            held.append(name)
            try: yield
            finally: held.pop()
        attempted=[]
        def confirm():
            self.assertEqual(held,['boot'])
            if not attempted:
                attempted.append(1)
                raise boot.HealthNotReady()
        def sleep(seconds):
            self.assertFalse(held)
        with patch.object(boot,'private_lock',lock), patch.object(boot,'confirm',confirm), patch.object(boot.time,'sleep',sleep):
            boot.confirm_when_ready(locked=True)

    def test_cpu_and_ssd_recovery_survive_wifi_failure(self):
        with patch.object(power,'recover_cpu') as cpu, patch('panasonic_on_ubuntu.ssd_power.restore') as ssd, \
                patch.object(peripherals,'restore',side_effect=RuntimeError('temporarily offline')):
            power.recover()
            cpu.assert_called_once()
            ssd.assert_called_once()

    def test_wifi_disappears_then_recovers_or_is_replaced(self):
        key='wifi:wlan0'
        p=self.root/'baseline'
        self.mock(peripherals,BASELINE=p)
        saved={key:dict(original='off',last='on',identity='pci-a')}
        for identity in ('pci-a','pci-b'):
            p.write_text(json.dumps(saved))
            with patch.object(peripherals,'devices',side_effect=[{}, {key:dict(value='on',identity=identity)}]), \
                    patch.object(peripherals,'write') as write:
                with self.assertRaises(RuntimeError): peripherals.restore()
                self.assertEqual(json.loads(p.read_text()),saved)
                peripherals.restore()
                self.assertEqual(write.call_count,int(identity=='pci-a'))
                self.assertFalse(p.exists())

    def test_private_lock_releases_and_bounds_wait(self):
        self.mock(private_lock,ROOT=self.root/'locks')
        with private_lock.private_lock('boot'):
            self.assertEqual((self.root/'locks'/'boot').stat().st_mode & 0o777,0o600)
            with self.assertRaises(TimeoutError), private_lock.private_lock('boot',0): pass
        with private_lock.private_lock('boot',0): pass

    def test_private_history_is_private_while_being_written(self):
        target=self.root/'history'
        actual=json.dump
        def inspect(value,stream,**opts):
            self.assertEqual(os.fstat(stream.fileno()).st_mode & 0o777,0o600)
            return actual(value,stream,**opts)
        with patch.object(care.json,'dump',side_effect=inspect):
            care.atomic(target,{'test':1})
        self.assertEqual(target.stat().st_mode & 0o777,0o600)

    def test_previous_directory_account_can_remove_its_own_enrollment(self):
        from types import SimpleNamespace
        user=SimpleNamespace(pw_name='employee',pw_uid=1200)
        marker=self.root/'1200'
        with patch.object(pam.face,'local_account',return_value=False):
            marker.write_text('employee')
            for action in ('disable','forget','sudo-off','polkit-1-off'):
                self.assertTrue(pam.face.management_allowed(user,action,marker))
            for action in ('enable','guided','enroll','supplement','polkit-1-on'):
                self.assertFalse(pam.face.management_allowed(user,action,marker))
            marker.write_text('different')
            self.assertFalse(pam.face.management_allowed(user,'forget',marker))
            marker.unlink()
            self.assertTrue(pam.face.management_allowed(user,'forget',marker))

    def test_stale_prediction_not_exposed_after_failed_update(self):
        path=self.root/'care'
        self.mock(care,RUNTIME=path,FIRMWARE=self.root)
        private=path.with_name('departure-prediction.json')
        private.write_text(json.dumps(dict(departure_at=time.time()+500)))
        for synced,mode,visible in ((True,'care',True),(False,'care',False),(True,'system',False)):
            path.write_text(json.dumps(dict(timestamp=time.time(),prediction_synced=synced,config=dict(mode=mode))))
            self.assertEqual(bool(care.status()['departure_prediction']),visible)

    def test_directory_only_account_retains_password_policy(self):
        from types import SimpleNamespace
        user=SimpleNamespace(pw_name='alice',pw_uid=1010)
        with patch.object(Path,'read_text',return_value='alice:x:1010:1010::/home/alice:/bin/sh\n'):
            self.assertTrue(pam.face.local_account(user))
            self.assertFalse(pam.face.local_account(SimpleNamespace(pw_name='other',pw_uid=1010)))
        with patch.object(Path,'read_text',return_value='alice:x:1011:1011::/home/alice:/bin/sh\n'):
            self.assertFalse(pam.face.local_account(user))

    def test_pam_checks_both_service_and_included_stacks(self):
        common='auth [success=1 default=ignore] pam_unix.so nullok\nauth requisite pam_deny.so\nauth required pam_permit.so\n'
        (self.root/'common-auth').write_text(common)
        account=self.root/'common-account'
        account.write_text('account required pam_unix.so\n')
        for service,hook in [('sudo',pam.HOOK),('polkit-1',pam.HOOK),('gdm-password',pam.face.HOOK)]:
            rows=hook+'@include common-auth\n@include common-account\n'
            self.assertTrue(pam.effective_policy(service,rows,self.root,True))
            for extra in ('auth required pam_u2f.so\n','auth substack site-auth\n','@include site-auth\n'):
                self.assertFalse(pam.effective_policy(service,rows+extra,self.root,True))
            self.assertFalse(pam.effective_policy(service,'@include common-auth\n'+hook,self.root,True))
            account.write_text('auth required site-policy.so\n')
            self.assertFalse(pam.effective_policy(service,rows,self.root,True))
            account.write_text('account required pam_unix.so\n')
        (self.root/'common-auth').write_text(common+'auth required site-policy.so\n')
        self.assertFalse(pam.effective_policy('sudo',pam.HOOK+'@include common-auth\n',self.root,True))

    def test_pose_config_eof_crlf_and_rejection(self):
        for text in ('[storage]','[storage]\r\n','[storage]\n\n[security]\nrequire_ir=true\n'):
            config=self.root/'config'
            db=self.root/'private.db'
            face_poses.staged_config(text,config,db)
            self.assertEqual(face_poses.tomllib.loads(config.read_text())['storage']['db_path'],str(db))
        with self.assertRaises(ValueError):
            face_poses.staged_config('[storage]\ndb_path="other"',config,db)

    def test_aborted_unpack_restores_old_source_and_services(self):
        script=(Path(__file__).resolve().parents[1]/'packaging/postrm').read_text()
        tx=self.root/'tx'
        backup=tx/'pou-sr3-fan-0.2.2'
        backup.mkdir(parents=True)
        (backup/'dkms.conf').write_text('old')
        (tx/'services').write_text('pou-power\npou-battery\n')
        sources=self.root/'sources'
        sources.mkdir()
        script=script.replace('/var/lib/oh-my-panasonic/driver-upgrade',str(tx)).replace('/usr/src',str(sources))
        sandbox=self.root/'commands'
        log=self.root/'services.log'
        sandbox.mkdir()
        for command in ('systemctl','udevadm'):
            tool=sandbox/command
            tool.write_text('#!/bin/sh\necho "$@" >>"'+str(log)+'"\n')
            tool.chmod(0o755)
        subprocess.run(['sh','-c',script,'postrm','abort-upgrade'],env=dict(os.environ,PATH=str(sandbox)+':'+os.environ['PATH']),check=True)
        self.assertEqual((sources/backup.name/'dkms.conf').read_text(),'old')
        self.assertIn('start pou-battery',log.read_text())
        self.assertIn('start pou-power',log.read_text())

    def test_esp_path_and_optional_data_are_separate(self):
        guid=uuid.uuid4()
        def node(t,s,payload):
            return struct.pack('<BBH',t,s,len(payload)+4)+payload
        hd=node(4,1,struct.pack('<IQQ',1,2048,65536)+guid.bytes_le+b'\2\2')
        name=node(4,4,(boot.LOADER+'\0').encode('utf-16le'))
        path=hd+name+node(127,255,b'')
        options=('--guard\0').encode('utf-16le')
        data=struct.pack('<IH',1,len(path))+b'X\0\0\0'+path+options
        boot.verify_entry(data,boot.LOADER,(1,guid),options)
        for args in [(boot.LOADER,(1,uuid.uuid4()),options),(boot.LOADER,(2,guid),options),
                     (boot.LOADER,(1,guid),b''),('--guard',(1,guid),None)]:
            with self.assertRaises(ValueError): boot.verify_entry(data,*args)

    def test_enabled_mmio_wrong_window_kept_intact(self):
        values={'enabled':'1','constraint_0_power_limit_uw':'28000000','constraint_1_power_limit_uw':'55000000',
                'constraint_0_time_window_us':'976','constraint_1_time_window_us':'1952'}
        for key,value in values.items(): (self.root/key).write_text(value)
        with self.assertRaises(RuntimeError): boot.normalize_mmio(self.root)
        self.assertEqual({p.name:p.read_text() for p in self.root.iterdir()},values)

    def test_backup_repair_requires_exact_mirror_and_generation(self):
        store=self.root/'store'
        disk=self.root/'disk'
        disk.mkdir()
        backup=Backup('CF-SR3SKAKS','V1.00L27',original())
        state=BootState(Phase.ACTIVE,3,backup.checksum,backup.sku,backup.bios)
        (disk/'state.bin').write_bytes(state.encode())
        self.mock(boot,STORE=store,DIRECTORY=disk,VARIABLES=self.root/'vars')
        with patch.object(boot,'owned_entry'),patch.object(boot,'config',return_value={'entry':3}), \
                patch.object(boot,'identity',return_value={'product_sku':backup.sku,'bios_version':backup.bios}):
            boot.mirror_backup(state,backup)
            (disk/'original.bin').write_bytes(b'partial')
            boot.repair_backup()
            self.assertEqual((disk/'original.bin').read_bytes(),backup.encode())
            (disk/'original.bin').unlink()
            r=store/'backup-generation.json'
            r.write_text(json.dumps(dict(json.loads(r.read_text()),generation=2)))
            with self.assertRaises(ValueError): boot.repair_backup()
            self.assertFalse((disk/'original.bin').exists())

    def test_dkms_two_kernel_failure_restores_old_installations(self):
        old=hardware.LEGACY[-2]
        new=hardware.BACKENDS['sr3']
        for failure in ('build','install','modprobe'):
            installed={(*old,'a'),(*old,'b')}
            log=[]
            once=[False]
            tx=self.root/failure
            def run(*args):
                log.append(args)
                if failure in args and not once[0] and ('b' in args or args[0]=='modprobe'):
                    once[0]=True
                    raise RuntimeError('injected')
                if args[0]=='dkms' and args[1] in ('install','uninstall'):
                    key=tuple(args[args.index(i)+1] for i in ('-m','-v','-k'))
                    if args[1]=='install':installed.add(key)
                    else:installed.discard(key)
            with patch.multiple(hardware,TX=tx,LOAD=self.root/'load',SOURCES=self.root/'sources'), \
                    patch.object(hardware,'run',side_effect=run),patch.object(hardware,'registered',return_value=True), \
                    patch.object(hardware,'installed_kernels',side_effect=lambda n,v:[k for x,y,k in installed if (x,y)==(n,v)]), \
                    patch.object(hardware,'kernel_targets',return_value=['a','b']),patch.object(hardware,'unload') as unload, \
                    patch.object(hardware,'selection',return_value=('sr3',['pou_sr3_fan','pou_battery'])):
                with self.assertRaises(RuntimeError):hardware.install()
                hardware.rollback()
                self.assertEqual(installed,{(*old,'a'),(*old,'b')})
                if failure=='build': unload.assert_not_called()
                self.assertFalse((tx/'switch.json').is_file())

    @unittest.skipUnless(shutil.which('cc'),'C compiler')
    def test_transition_uses_real_backend_supply_predicate(self):
        source=(Path(__file__).resolve().parents[1]/'kernel/pou_battery.c').read_text()
        def function(start,end):
            return source[source.index(start):source.index(end,source.index(start))]
        predicate=function('static bool external_supply_online','static DECLARE_DELAYED_WORK')
        transition=function('static int transition_begin','static int transition_end')
        c=self.root/'test.c'
        c.write_text("""#include <stdbool.h>
#include <assert.h>
#include <string.h>
#include <errno.h>
typedef unsigned char u8;
enum {POWER_SUPPLY_PROP_ONLINE, POWER_SUPPLY_PROP_SCOPE, POWER_SUPPLY_PROP_PRESENT, POWER_SUPPLY_SCOPE_SYSTEM};
static int ac,usb,scope,present,writes,armed_transition,jiffies,arm_deadline=100;
static bool offline_held,owned; static const char *last_transition;
#define time_before(a,b) ((a)<(b))
#define pr_info(...) ((void)0)
static int restore_retry_locked(void){return 0;}
static int supply_value(const char*n,int p){if(!strcmp(n,"AC"))return ac;if(!strcmp(n,"BAT1"))return present;return p==POWER_SUPPLY_PROP_SCOPE?scope:usb;}
static int battery_command(int a,int b,int c,u8*r){++writes;*r=1;return 0;}
"""+predicate+transition+"""
int main(void){for(int event=1;event<=2;event++)for(ac=0;ac<=1;ac++)
 for(usb=0;usb<=2;usb++)for(present=0;present<=1;present++)for(scope=2;scope<=3;scope++){
 armed_transition=event;writes=0;assert(transition_begin(event)==0);
 assert(writes==(present && (ac || (usb && scope==POWER_SUPPLY_SCOPE_SYSTEM))));}return 0;}
""")
        exe=self.root/'test'
        subprocess.run(['cc','-std=c11',str(c),'-o',str(exe)],check=True)
        subprocess.run([exe],check=True)
