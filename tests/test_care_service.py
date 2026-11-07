"""Run the real daemon against temporary supply files and a simulated clock."""
import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from panasonic_on_ubuntu import care


class ServiceTest(unittest.TestCase):
    def exercise(self, *, supported=True, mode='care', charging=False, recovery_failure=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            locations={name:root/name for name in ('CONFIG','STATE','RUNTIME','FIRMWARE','SUPPLIES','LOG_DIR')}
            firmware=locations['FIRMWARE']; firmware.mkdir()
            if supported:
                for name,value in {'interface_version':'1','mode':'0','ac_override':'0'}.items():
                    (firmware/name).write_text(value)
            supplies=locations['SUPPLIES']
            (supplies/'BAT1').mkdir(parents=True); (supplies/'AC').mkdir()
            (supplies/'AC/online').write_text('1')
            fields={'CAPACITY':90,'PRESENT':1,'STATUS':'Charging' if charging else 'Not charging',
                    'POWER_NOW':5000000 if charging else 0,'ENERGY_NOW':36000000}
            (supplies/'BAT1/uevent').write_text('\n'.join('POWER_SUPPLY_'+key+'='+str(value) for key,value in fields.items()))
            locations['CONFIG'].write_text(json.dumps({'mode':mode,'limit':80,'maintenance_days':0,'request_id':'test'}))
            now=[1000]; handlers={}; observed=[]
            real_restore=care.restore
            def restore():
                # Initialization/exit still work; fail only the recovery after
                # current verification detects a real mismatch.
                if recovery_failure and 1025<=now[0]<1040: raise OSError('simulated EC I/O error')
                real_restore()
            def sleep(seconds):
                now[0]+=seconds
                if locations['RUNTIME'].exists(): observed.append(json.loads(locations['RUNTIME'].read_text()))
                if now[0]>=1040: handlers[signal.SIGTERM](signal.SIGTERM,None)
            with patch.multiple(care,**locations), patch.object(care.time,'time',side_effect=lambda:now[0]), \
                    patch.object(care.Wakeup,'wait',side_effect=sleep), patch.object(care.signal,'signal',side_effect=lambda sig,fn:handlers.update({sig:fn})), \
                    patch.object(care,'notify'), patch.object(care,'restore',side_effect=restore), \
                    patch('panasonic_on_ubuntu.charge_habits.HISTORY',root/'habits.json'):
                care.run()
            state=json.loads(locations['STATE'].read_text())
            final_mode=(firmware/'mode').read_text() if supported else None
            return observed,state,final_mode

    def test_hold_is_acknowledged_and_stop_releases_it(self):
        observed,state,mode=self.exercise()
        self.assertTrue(all(x['phase']=='holding' and x['driver_mode']==1 and not x['error'] for x in observed))
        self.assertEqual(state['request_id'],'test')
        self.assertEqual(mode,'0')

    def test_learning_storage_failure_does_not_release_charge_cap(self):
        with patch('panasonic_on_ubuntu.charge_habits.Habits.observe',side_effect=OSError('disk full')):
            observed,_,_=self.exercise()
        self.assertTrue(all(x['phase']=='holding' and x['driver_mode']==1 and not x['error'] for x in observed))
        self.assertTrue(all(x['departure_prediction'] is None for x in observed))

    def test_continuing_charge_current_latches_failure_and_releases(self):
        observed,state,mode=self.exercise(charging=True)
        self.assertEqual(observed[0]['phase'],'holding-pending')
        self.assertEqual(observed[-1]['phase'],'error')
        self.assertIn('verification_error',state)
        self.assertEqual(observed[-1]['driver_mode'],0)
        self.assertEqual(mode,'0')

    def test_failed_restoration_never_claims_automatic_mode(self):
        observed,state,mode=self.exercise(charging=True,recovery_failure=True)
        self.assertEqual(observed[-1]['phase'],'recovery-failed')
        self.assertEqual(observed[-1]['driver_mode'],1)
        self.assertEqual(observed[-1]['label'],'自动恢复尚未确认')
        self.assertIn('恢复失败',observed[-1]['error'])
        self.assertEqual(mode,'0') # The final cleanup succeeds after the fault.

    def test_disable_without_optional_module_is_acknowledged(self):
        observed,state,_=self.exercise(supported=False,mode='system')
        self.assertEqual(state['request_id'],'test')
        self.assertEqual(observed[-1]['phase'],'system')
        self.assertEqual(observed[-1]['error'],'')

    def test_unavailable_control_reports_failure_for_matching_request(self):
        observed,state,_=self.exercise(supported=False)
        self.assertEqual(state['request_id'],'test')
        self.assertEqual(observed[-1]['phase'],'unsupported')
        self.assertTrue(observed[-1]['error'])


if __name__=='__main__': unittest.main()
