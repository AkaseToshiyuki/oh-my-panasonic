from pathlib import Path
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from panasonic_on_ubuntu import face
from panasonic_on_ubuntu.battery import snapshot
from panasonic_on_ubuntu.power_summary import describe


class PowerPresentationTests(unittest.TestCase):
    def test_low_pd_power_explains_waiting_without_overriding_battery_state(self):
        snapshot = {'power_input':{'source':'typec', 'mains_online':False, 'charging_minimum_w':27,
                                  'ports':[{'protocol':'PD', 'contract_w':20.04}]},
                    'batteries':[{'fields':{'status':{'value':'Not charging'}, 'capacity':{'value':'75'}},
                                  'metrics':{'battery_power_w':0}}]}
        care = {'phase':'charging', 'driver_mode':0, 'config':{'mode':'care', 'limit':80}}
        self.assertEqual(describe(snapshot, care)['status'], 'PD 供电不足，等待充电')
        self.assertEqual(describe(snapshot, care)['power'], '')
        for watts in (None, 0, 27, 45):
            changed = deepcopy(snapshot)
            changed['power_input']['ports'][0]['contract_w'] = watts
            self.assertEqual(describe(changed, care)['status'], '等待充电')
        for update in ({'source':'dc'}, {'mains_online':True}, {'charging_minimum_w':None}, {'ports':[]},
                       {'ports':snapshot['power_input']['ports'] * 2}):
            changed = deepcopy(snapshot)
            changed['power_input'].update(update)
            self.assertNotIn('PD 供电不足', describe(changed, care)['status'])
        for update in ({'phase':'holding', 'driver_mode':1}, {'phase':'maintenance-pause', 'driver_mode':1},
                       {'stale':True}, {'error':'I/O failure'}):
            self.assertNotIn('PD 供电不足', describe(snapshot, {**care, **update})['status'])
        for status in ('Charging', 'Discharging', 'Full'):
            changed = deepcopy(snapshot)
            changed['batteries'][0]['fields']['status']['value'] = status
            self.assertNotIn('PD 供电不足', describe(changed, care)['status'])

    def summary(self, source, status, watts=None, target=100, **values):
        fields = {key:{'value':str(value)} for key,value in dict(values,status=status).items()}
        battery = {'fields':fields, 'metrics':{'battery_power_w':watts}}
        return describe({'batteries':[battery], 'power_input':{'source':source,'contract_w':65}},
                        {'phase':'holding','config':{'limit':target,'mode':'care'}})

    def test_charge_estimate_to_limit_does_not_use_time_to_full(self):
        data=self.summary('typec','Charging',10,80,capacity=50,energy_now=20000000,
                          energy_full=40000000,power_now=10000000,time_to_full_now=100)
        self.assertEqual(data['source'],'USB-C 供电')
        self.assertIn('1 小时 12 分钟',data['status'])
        self.assertEqual(data['power'],'电池充入 10 W')

    def test_charged_and_at_limit(self):
        self.assertIn('80%',self.summary('dc','Not charging',target=80,capacity=81)['status'])
        self.assertEqual(self.summary('dc','Full',capacity=100)['status'],'已充满')

    def test_battery_power_rows_remain_separate_from_adapter_input(self):
        for source in ('battery', 'dc', 'typec'):
            for status, charge, discharge in [('Charging', '8.5 W', ''), ('Discharging', '', '8.5 W'),
                                               ('Full', '', ''), ('Not charging', '', '')]:
                snapshot = {'power_input':{'source':source, 'measured_input_w':30},
                            'batteries':[{'fields':{'status':{'value':status}},
                                          'metrics':{'battery_power_w':8.5}}]}
                data = describe(snapshot, {})
                self.assertEqual(data['charge_power'], charge)
                self.assertEqual(data['discharge_power'], discharge)
        self.assertEqual(self.summary('dc', 'Charging', 0)['charge_power'], '0 W')
        self.assertEqual(self.summary('battery', 'Discharging', None)['discharge_power'], '')

    def test_discharge_when_adapter_is_present(self):
        data=self.summary('dc','Discharging',5,capacity=47,energy_now=15000000,power_now=5000000)
        self.assertIn('DC 已连接',data['source'])
        self.assertEqual(data['power'],'放电 5 W')
        self.assertEqual(data['status'],'预计可用 3 小时')
        self.assertNotIn('65',data['summary'])

    def test_missing_battery_and_reserved_temperature(self):
        self.assertIn('未检测到', describe({}, {})['status'])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            pack=root/'BAT1'; pack.mkdir()
            for field,value in {'type':'Battery','present':'1','cycle_count':'0'}.items():
                (pack/field).write_text(value)
            (root/'battery_charge_index').write_text('176')
            (root/'battery_temp_decikelvin').write_text('2996')
            metrics=snapshot(root, root)['batteries'][0]['metrics']
            self.assertEqual(metrics['charge_index'],176)
            self.assertEqual(metrics['temperature_c'],26.45)
            (root/'battery_temp_decikelvin').write_text('65535')
            self.assertNotIn('temperature_c',snapshot(root,root)['batteries'][0]['metrics'])


class FaceScopeTests(unittest.TestCase):
    def test_opt_in_reversible_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            pam=Path(directory)/'gdm-password'
            original='auth requisite pam_nologin.so\n@include common-auth\n'
            pam.write_text(original)
            with patch.object(face,'PAM',pam):
                face.edit_pam(True)
                face.edit_pam(True)
                self.assertEqual(pam.read_text().count(face.HOOK),1)
                face.edit_pam(False)
                self.assertEqual(pam.read_text(),original)

    def test_other_pam_services_rejected_without_camera(self):
        with patch.dict('os.environ', {'PAM_SERVICE':'sudo','PAM_TYPE':'auth'}, clear=True), patch.object(face,'call') as backend:
            self.assertFalse(face.check_locked())
            backend.assert_not_called()
