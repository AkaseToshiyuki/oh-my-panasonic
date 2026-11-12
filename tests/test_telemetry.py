import io
import json
import unittest
from unittest.mock import patch
from panasonic_on_ubuntu import telemetry


class CollectionTests(unittest.TestCase):
    def test_cpu_page_never_probes_unrelated_devices_or_history(self):
        with patch.object(telemetry, 'power_status', return_value={'mode':'system'}) as power, \
                patch.object(telemetry, 'system', return_value={'profile':'balanced'}), \
                patch.object(telemetry, 'peripheral_status', return_value={}), \
                patch('panasonic_on_ubuntu.ssd_power.status', return_value={'available':True}), \
                patch.object(telemetry, 'optical', return_value={'supported':False}), \
                patch.multiple(telemetry, snapshot=unittest.mock.DEFAULT, network=unittest.mock.DEFAULT,
                               audio=unittest.mock.DEFAULT, firmware_status=unittest.mock.DEFAULT,
                               history=unittest.mock.DEFAULT) as unused:
            value = telemetry.Collector().collect('cpu')
        power.assert_called_once()
        self.assertEqual(set(value), {'timestamp','scope','errors','power','peripherals','system','ssd_power','optical'})
        self.assertTrue(value['ssd_power']['available'])
        for function in unused.values():function.assert_not_called()

    def test_history_is_bounded_cache_but_live_battery_is_always_read(self):
        collector = telemetry.Collector()
        with patch.object(telemetry, 'snapshot', return_value={'batteries':[]}) as battery, \
                patch('panasonic_on_ubuntu.care.status', return_value={}), \
                patch.object(telemetry, 'history', return_value=[]) as history, \
                patch.object(telemetry.time, 'monotonic', side_effect=[1, 6, 30, 31, 0]):
            for unused in range(5):collector.collect('battery')
        self.assertEqual(history.call_count, 3)
        self.assertEqual(battery.call_count, 5)

    def test_history_failure_does_not_hide_current_battery(self):
        with patch.object(telemetry, 'snapshot', return_value={'batteries':[]}), \
                patch('panasonic_on_ubuntu.care.status', return_value={}), \
                patch.object(telemetry, 'history', side_effect=OSError('read failure')):
            value = telemetry.Collector().collect('battery')
        self.assertIn('battery', value)
        self.assertIn('power_summary', value)
        self.assertEqual(value['errors']['history'], 'read failure')

    def test_device_failure_is_not_replaced_with_cached_success(self):
        collector = telemetry.Collector()
        with patch.object(telemetry, 'audio', side_effect=[{'available':True}, RuntimeError('disconnected')]):
            self.assertTrue(collector.collect('audio')['audio']['available'])
            value = collector.collect('audio')
        self.assertEqual(value['audio'], {})
        self.assertEqual(value['errors']['audio'], 'disconnected')

    def test_invalid_scope_fails_before_any_probe(self):
        with patch.object(telemetry, 'snapshot') as battery:
            with self.assertRaises(ValueError):telemetry.collect('unknown')
        battery.assert_not_called()

    def test_stream_reads_on_request_and_exits_at_eof(self):
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO('cpu\nbattery\n')), patch('sys.stdout', output), \
                patch.object(telemetry, 'collect', side_effect=lambda scope: {'scope':scope}) as collect:
            telemetry.stream()
        self.assertEqual([call.args[0] for call in collect.call_args_list], ['cpu','battery'])
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()],
                         [{'scope':'cpu'}, {'scope':'battery'}])

    def test_stream_rejects_unbounded_or_unknown_requests(self):
        for value in ('x'*100+'\n', 'all\n', 'cpu'):
            with patch('sys.stdin', io.StringIO(value)), patch.object(telemetry, 'collect') as collect:
                with self.assertRaises(ValueError):telemetry.stream()
            collect.assert_not_called()

