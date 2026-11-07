import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from panasonic_on_ubuntu import pd_request as pd
from panasonic_on_ubuntu.power_input import detect


RAW = '0x000000000003c0a70002d0de0801912c'
STATUS = '0x000000000000000033029ca7402b5b26'


class PDFallbackTests(unittest.TestCase):
    def test_power_service_does_not_issue_raw_pd_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)/'pd.json'
            cache.write_text(json.dumps({'rdo':0x33029ca7,'timestamp':time.time()}))
            with patch.object(pd,'CACHE',cache), patch.object(pd,'sample') as probe, \
                    patch.object(pd,'query') as query:
                self.assertIsNone(pd.start(lambda *args: self.fail('unexpected cache update')))
                self.assertIsNone(pd.start(lambda *args: self.fail('unexpected cache update')))
                probe.assert_not_called()
                query.assert_not_called()
            self.assertFalse(cache.exists())

    def test_verified_fixed_pdos_and_rdo_decode_to_current_contract(self):
        words = pd.decode_pdos(RAW)
        self.assertEqual(pd.cached_profiles({'pdos': words}), [
            {'position':1,'volts':5,'amps':3}, {'position':2,'volts':9,'amps':2.22},
            {'position':3,'volts':12,'amps':1.67}])
        self.assertEqual(((pd.status_request(STATUS) >> 10) & 0x3ff)/100, 1.67)
        with self.assertRaises(ValueError): pd.decode_pdos('0x12')
        with self.assertRaises(ValueError): pd.decode_pdos('0x000000000003c0a7000000000801912c')
        self.assertEqual(pd.cached_profiles({'pdos':[0xffffffff]}), [])

    def test_cached_partner_profile_overrides_stale_kernel_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supply = root/'supplies/ucsi-source-psy-USBC000:001'
            supply.mkdir(parents=True)
            (supply/'uevent').write_text('POWER_SUPPLY_SCOPE=System\nPOWER_SUPPLY_ONLINE=1\n'
                'POWER_SUPPLY_USB_TYPE=C [PD] PD_PPS\nPOWER_SUPPLY_VOLTAGE_NOW=5000000\nPOWER_SUPPLY_CURRENT_NOW=3000000')
            controller = root/'controller'; controller.mkdir()
            port = root/'typec/port0'; port.mkdir(parents=True)
            (supply/'device').symlink_to(controller, target_is_directory=True)
            (port/'device').symlink_to(controller, target_is_directory=True)
            partner = root/'typec/port0-partner'; partner.mkdir()
            cache = root/'pd.json'
            cache.write_text(json.dumps({'partner':pd.partner_id(partner), 'timestamp':time.time(),
                'rdo':pd.status_request(STATUS), 'pdos':pd.decode_pdos(RAW)}))
            result = detect(root/'supplies', root/'typec', cache)
            self.assertEqual(result['voltage_label'], '12 V')
            self.assertEqual(result['ports'][0]['contract_w'], 20.04)
            self.assertEqual(result['profiles_label'], '5 V/3 A、9 V/2.22 A、12 V/1.67 A')
            self.assertIsNone(result['measured_input_w'])

    def test_query_rejects_changed_partner_request_or_inconsistent_pdos(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            port, supply, partner = root/'port', root/'supply', root/'partner'
            for node in (port,supply,partner): node.mkdir()
            for node, field, value in ((port,'power_operation_mode','usb_power_delivery'),
                    (port,'power_role','source [sink]'), (supply,'online','1'),
                    (supply,'usb_type','[PD]'), (supply,'voltage_now','12000000')):
                (node/field).write_text(value)
            with patch.multiple(pd,PORT=port,SUPPLY=supply,PARTNER=partner), \
                    patch.object(pd,'query',side_effect=[STATUS,RAW,RAW,STATUS]):
                self.assertEqual(pd.sample()['pdos'], pd.decode_pdos(RAW))
            with patch.multiple(pd,PORT=port,SUPPLY=supply,PARTNER=partner), \
                    patch.object(pd,'query',side_effect=[STATUS,RAW,RAW,STATUS.replace('33029','23029')]):
                self.assertIsNone(pd.sample())
            with patch.multiple(pd,PORT=port,SUPPLY=supply,PARTNER=partner), \
                    patch.object(pd,'query',side_effect=[STATUS,RAW,RAW.replace('0003c0a7','0003c0a6'),STATUS]):
                self.assertEqual(pd.sample()['pdos'], [])
            with patch.multiple(pd,PORT=port,SUPPLY=supply,PARTNER=partner), \
                    patch.object(pd,'query',side_effect=[STATUS,RAW,RAW,STATUS]), \
                    patch.object(pd,'partner_id',side_effect=['first','second']):
                self.assertIsNone(pd.sample())

    def test_second_chunk_error_preserves_only_verified_prefix(self):
        first = '0x0004b0640003c0a70002d0de0801912c'
        with patch.object(pd,'query',side_effect=[first,OSError('firmware')]):
            self.assertEqual(pd.query_pdos(), pd.decode_pdos(first))
