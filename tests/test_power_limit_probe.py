import contextlib
import copy
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

spec = importlib.util.spec_from_file_location('power_limit_probe', Path(__file__).resolve().parents[1] / 'tools/inspect_power_limits.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class PowerLimitProbeTests(unittest.TestCase):
    def raw(self):
        return {'0x606': '0xa0e03', '0x610': '0x804281b800dd80e0',
                '0x614': hex((440 << 32) | (56 << 16) | 224)}

    def snapshot(self):
        return {'schema': 1, 'read_only': True, 'boot_id': 'boot-a',
                'dmi': {'sys_vendor': 'Panasonic', 'product_name': 'CFSR3-1', 'product_sku': 'test', 'bios_version': 'V1.00L12'},
                'cpu': {'vendor_id': 'GenuineIntel', 'cpu family': '6', 'model': '154'},
                'raw_msrs': self.raw(), 'decoded_msrs': probe.decode_msrs(self.raw()),
                'settings': {name: {'present': True, 'sha256': 'a'} for name in probe.SETTINGS},
                'firmware': {'CpuSetup': {'available': True, 'sha256': 'a'},
                             'Setup': {'available': True, 'sha256': 'b'}},
                'rapl': {'intel-rapl-mmio:0': {'constraint_0_power_limit_uw': '15000000',
                                            'constraint_1_power_limit_uw': '28000000'}},
                'errors': [], 'temperature_c': 30}

    def test_measured_sr3_lock_and_limits(self):
        decoded = probe.decode_msrs(self.raw())
        self.assertTrue(decoded['locked'])
        self.assertTrue(decoded['pl1']['enabled'])
        self.assertEqual(decoded['pl1']['watts'], 28)
        self.assertEqual(decoded['pl2']['watts'], 55)
        self.assertEqual(decoded['pl1']['window_seconds'], 28)
        self.assertEqual(decoded['sku_min_w'], 7)
        self.assertEqual(decoded['sku_max_w'], 55)

    def test_zero_sku_bound_is_unspecified_not_zero_watts(self):
        raw = self.raw(); raw['0x614'] = '0xe0'
        self.assertIsNone(probe.decode_msrs(raw)['sku_max_w'])
        for invalid in (-1, 1 << 64, True, 1.5):
            with self.assertRaises(ValueError):
                probe.uint64(invalid)

    def test_msr_reader_only_opens_read_mode_and_known_offsets(self):
        device = MagicMock()
        device.__enter__.return_value = device
        device.read.side_effect = lambda count: int(self.raw()[hex(device.seek.call_args.args[0])], 0).to_bytes(count, 'little')
        with patch.object(Path, 'open', return_value=device) as opened:
            result = probe.read_msrs(Path('/dev/cpu/0/msr'))
        opened.assert_called_once_with('rb', buffering=0)
        self.assertEqual(device.seek.call_args_list, [call(0x606), call(0x610), call(0x614)])
        self.assertEqual(result['0x610'], self.raw()['0x610'])
        with patch.object(Path, 'open', return_value=io.BytesIO(b'')):
            with self.assertRaises(OSError):
                probe.read_msrs(Path('/dev/cpu/0/msr'))

    def test_default_cli_does_not_touch_hardware(self):
        with patch.object(probe, 'collect', side_effect=AssertionError('hardware accessed')):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                probe.main([])
        self.assertIn('--run', out.getvalue())

    def test_never_grants_writes_even_when_unlocked_and_cool(self):
        for temperature in (30, 85, None, float('nan')):
            snapshot = self.snapshot(); snapshot['temperature_c'] = temperature
            snapshot['decoded_msrs']['locked'] = False
            result = probe.assessment(snapshot)
            self.assertFalse(result['hardware_writes_permitted'])
            self.assertEqual(result['cutoff_c'], 85)
            if temperature == 85:
                self.assertIn('temperature_at_or_above_85c', result['reasons'])

    def test_restore_comparison_detects_changes_and_incomplete_evidence(self):
        before = self.snapshot(); after = copy.deepcopy(before); after['boot_id'] = 'boot-b'
        self.assertEqual(probe.compare(before, after)['status'], 'matched')
        self.assertTrue(probe.compare(before, after)['different_boot'])
        after['settings']['power.json']['sha256'] = 'changed'
        self.assertIn('settings', probe.compare(before, after)['differences'])
        for snapshot in (before, after):
            snapshot['settings'] = {}; snapshot['raw_msrs'] = {}
        self.assertEqual(probe.compare(before, after)['status'], 'incomplete')

    def test_unknown_firmware_is_not_decoded_using_l27_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {'proc/cpuinfo': b'vendor_id : GenuineIntel\ncpu family : 6\nmodel : 999\n',
                     'sys/class/dmi/id/bios_version': b'V1.00L12',
                     f'sys/firmware/efi/efivars/CpuSetup-{probe.CPU_GUID}': b'\x07\0\0\0' + b'\0' * 0x3c1}
            for relative, data in files.items():
                path = root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            with patch.object(probe, 'read_msrs', side_effect=AssertionError('unsupported CPU')):
                result = probe.collect(root)
        self.assertFalse(result['firmware']['CpuSetup']['layout_verified_on_device'])
        self.assertNotIn('power_lock', result['firmware']['CpuSetup'])

    def test_output_never_overwrites_a_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'baseline.json'
            probe.write_new(path, {'value': 'original'})
            with self.assertRaises(FileExistsError):
                probe.write_new(path, {'value': 'replacement'})
            self.assertEqual(probe.load_snapshot(path)['value'], 'original')

    def test_malformed_evidence_is_rejected(self):
        for field, value in [('firmware', None), ('raw_msrs', {'0x610': '0'}), ('errors', None)]:
            broken = self.snapshot(); broken[field] = value
            with self.assertRaises(ValueError):
                probe.compare(self.snapshot(), broken)

    def test_missing_mmio_evidence_cannot_pass_restore_check(self):
        a = self.snapshot(); b = self.snapshot()
        a['rapl'] = {}; b['rapl'] = {}
        self.assertEqual(probe.compare(a, b)['status'], 'incomplete')

    def test_backup_checks_changes_and_corruption_without_restore_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'; root.mkdir()
            for relative in probe.backup_files().values():
                path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'\x07\0\0\0' + b'original')
            snapshot = probe.collect(root)
            target = Path(directory) / 'backup'
            self.assertTrue(probe.backup(target, snapshot, root)['verified'])
            self.assertEqual(probe.verify_backup(target)['status'], 'verified')
            (target / 'power.json').write_bytes(b'corrupted')
            with self.assertRaises(ValueError):
                probe.verify_backup(target)
            (root / probe.backup_files()['fan.json']).write_bytes(b'changed')
            with self.assertRaises(ValueError):
                probe.backup(Path(directory) / 'changed-baseline', snapshot, root)
            with self.assertRaises(FileExistsError):
                probe.backup(target, snapshot, root)


if __name__ == '__main__':
    unittest.main()
