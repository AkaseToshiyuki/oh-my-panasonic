from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json
import time

from panasonic_on_ubuntu.power_input import detect
from panasonic_on_ubuntu.pd_request import partner_id, status_request


class PowerInputTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.supplies = self.root/'power_supply'
        self.typec = self.root/'typec'
        self.supply = self.supplies/'ucsi-source-psy-USBC000:001'
        self.supply.mkdir(parents=True)
        self.supply.joinpath('type').write_text('USB')
        ac = self.supplies/'AC'
        ac.mkdir()
        (ac/'type').write_text('Mains')
        (ac/'online').write_text('1')

    def contract(self, online='1', extra=''):
        (self.supply/'uevent').write_text(
            f'POWER_SUPPLY_ONLINE={online}\nPOWER_SUPPLY_SCOPE=System\n'
            'POWER_SUPPLY_USB_TYPE=C [PD] PD_PPS\nPOWER_SUPPLY_VOLTAGE_NOW=20000000\n'
            'POWER_SUPPLY_CURRENT_NOW=2250000\nPOWER_SUPPLY_CURRENT_MAX=3000000\n'+extra)

    def test_contract_is_not_an_actual_input_power_measurement(self):
        self.contract()
        data = detect(self.supplies, self.typec)
        self.assertEqual(data['ports'][0]['contract_w'], 45)
        self.assertIsNone(data['ports'][0]['measured_input_w'])
        self.assertEqual(data['label'], 'USB-C PD')
        self.contract(extra='POWER_SUPPLY_POWER_NOW=7200000')
        self.assertEqual(detect(self.supplies,self.typec)['ports'][0]['measured_input_w'],7.2)

    def test_stale_contract_ignored_on_unplug(self):
        self.contract('0')
        result = detect(self.supplies, self.typec)
        self.assertFalse(result['ports'])
        self.assertTrue(result['mains_online'])
        (self.supplies/'AC/online').write_text('0')
        self.assertEqual(detect(self.supplies,self.typec)['label'], '电池供电')

    def test_sv7_pd_request_is_scoped_to_attached_partner_and_never_metering(self):
        self.contract()
        controller = self.root/'controller'
        controller.mkdir()
        port = self.typec/'port0'
        port.mkdir(parents=True)
        (self.supply/'device').symlink_to(controller, target_is_directory=True)
        (port/'device').symlink_to(controller, target_is_directory=True)
        partner = self.typec/'port0-partner'
        partner.mkdir()
        (port/'power_operation_mode').write_text('usb_power_delivery')
        event = self.supply/'uevent'
        event.write_text(event.read_text().replace('C [PD]', '[C] PD').replace('20000000','5000000'))
        cache = self.root/'request.json'
        request = status_request('0x000000000000000033029ca7402b5b26')
        self.assertEqual(request, 0x33029ca7)
        self.assertIsNone(status_request('0x000000000000000033029ca7403b5b26'))
        data = {'timestamp':time.time(), 'partner':partner_id(partner), 'rdo':request}
        cache.write_text(json.dumps(data))
        result = detect(self.supplies,self.typec,cache)
        self.assertEqual(result['contract_label'],'第 3 档')
        self.assertIsNone(result['ports'][0]['volts'])
        self.assertIsNone(result['ports'][0]['amps'])
        self.assertIsNone(result['measured_input_w'])
        for invalid in ({'timestamp':time.time()-100}, {'partner':'other'}):
            cache.write_text(json.dumps({**data, **invalid}))
            self.assertEqual(detect(self.supplies,self.typec,cache)['contract_label'],'PD 已连接')
        event.write_text(event.read_text().replace('ONLINE=1','ONLINE=0'))
        self.assertFalse(detect(self.supplies,self.typec,cache)['ports'])

    def test_online_typec_is_separate_from_logical_battery_operation(self):
        self.contract()
        (self.supplies/'AC/online').write_text('0')
        data = detect(self.supplies,self.typec)
        self.assertFalse(data['mains_online'])
        self.assertEqual(data['path_label'],'外部电源')
        self.assertEqual(data['ports'][0]['contract_w'],45)

    def test_only_an_unambiguous_partner_can_supply_the_pdo_number(self):
        self.contract()
        controller = self.root/'controller'
        controller.mkdir()
        port = self.typec/'port0'
        port.mkdir(parents=True)
        try:
            (self.supply/'device').symlink_to(controller, target_is_directory=True)
            (port/'device').symlink_to(controller, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        profile = self.typec/'port0-partner/usb_power_delivery/source-capabilities/5:fixed_supply'
        profile.mkdir(parents=True)
        (profile/'voltage').write_text('20000mV')
        (profile/'maximum_current').write_text('2250mA')
        self.assertEqual(detect(self.supplies,self.typec)['ports'][0]['fixed_pdo'],5)
        other = self.typec/'port1'
        other.mkdir()
        (other/'device').symlink_to(controller, target_is_directory=True)
        self.assertIsNone(detect(self.supplies,self.typec)['ports'][0]['fixed_pdo'])
