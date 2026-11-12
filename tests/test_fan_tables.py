import hashlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from panasonic_on_ubuntu import fan, fan_tables


def field(value):
    if isinstance(value, str):
        data = value.encode() + b'\0'
        return struct.pack('<IQ', 8, len(data)) + data
    return struct.pack('<IQ', 4, value)


def repository(points=((3132, 40), (3332, 100))):
    body = b''
    for mode in ('normal', 'slow', 'fast'):
        values = [1, 1]
        for temp, percent in points:
            values += ['\\_SB_.PC00.LPCB.EC0_.SEN4', temp, '\\_SB_.PC00.LPCB.EC0_.TFN1', 393216, percent, 1, 30]
        value = b''.join(field(v) for v in values)
        key = f'/shared/tables/acpr/fan_{mode}'.encode() + b'\0'
        body += struct.pack('<HII', 0xa0d8, 0, len(key)) + key + struct.pack('<II', 7, len(value)) + value
    return (struct.pack('<HHII', 0x1fe5, 148, 0x02000000, 0) + bytes(96)
            + hashlib.sha256(body).digest() + struct.pack('<I4s', len(body), b'KEYS') + body)


class FanTableTests(unittest.TestCase):
    def test_cached_sensor_keeps_reading_live_temperature_and_rejects_replacement(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(fan_tables,'THERMAL',Path(folder)):
            zone=Path(folder)/'thermal_zone0';zone.mkdir()
            (zone/'type').write_text('SEN4');(zone/'temp').write_text('40000')
            reader=fan_tables.TemperatureReader()
            with patch.object(reader,'discover',wraps=reader.discover) as discover:
                self.assertEqual(reader.read(['SEN4']),{'SEN4':40})
                (zone/'temp').write_text('80000')
                self.assertEqual(reader.read(['SEN4']),{'SEN4':80})
                discover.assert_called_once()
                zone.rename(Path(folder)/'removed')
                zone.mkdir();(zone/'type').write_text('different');(zone/'temp').write_text('20000')
                with self.assertRaises(ValueError):reader.read(['SEN4'])
                self.assertFalse(reader.paths)

    def test_read_real_format_and_reject_corruption(self):
        blob = repository()
        self.assertEqual(fan_tables.parse(blob)['slow']['SEN4'], [(40, 40), (60, 100)])
        for bad in (blob[:-1], blob[:148] + bytes(len(blob)-148), repository(((3332, 60), (3132, 100)))):
            with self.assertRaises(ValueError):
                fan_tables.parse(bad)

    def test_interpolation_and_missing_sensor(self):
        points = [(40, 40), (60, 100)]
        self.assertEqual(fan_tables.interpolate(30, points), 20)
        self.assertEqual(fan_tables.interpolate(50, points), 70)
        self.assertEqual(fan_tables.interpolate(70, points), 100)
        with tempfile.TemporaryDirectory() as folder, patch.object(fan_tables, 'THERMAL', Path(folder)):
            with self.assertRaises(ValueError):
                fan_tables.temperatures(['SEN4'])

    def test_presets_are_read_only_and_follow_resolves_base(self):
        tables = fan_tables.parse(repository())
        for mode in fan.MODES:
            config = {**fan.DEFAULT, 'mode': mode}
            view = fan.plot(config, 1, tables, True)
            self.assertTrue(view['series'])
            self.assertEqual(view['editable'], mode == 'custom')
        self.assertEqual(fan.plot(fan.DEFAULT, 1, tables)['mode'], 'quiet')
        self.assertEqual(fan.plot(fan.DEFAULT, 0, tables)['mode'], 'standard')
        self.assertTrue(fan.plot({**fan.DEFAULT, 'mode': 'standard'}, 0)['firmware'])

    def test_manual_plot_tracks_slider_and_hot_override(self):
        view = fan.plot({**fan.DEFAULT, 'mode': 'manual', 'percent': 44}, 0)
        self.assertEqual(view['series'][0]['points'][0][1], 44)
        self.assertIn((82,100), view['series'][0]['points'])

    def test_platform_sensor_governs_cooling_independently_of_cpu(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); config=root/'config'
            config.write_text(json.dumps({'mode':'standard'}))
            for name,value in {'fan_feedback_percent':20,'fan_profile':0,'package_temperature_c':30,
                               'fan_pwm':0,'software_quiet_curve':1}.items():
                (root/name).write_text(str(value))
            with patch.object(fan,'FIRMWARE',root),patch.object(fan,'CONFIG',config),patch.object(fan_tables,'load',return_value=fan_tables.parse(repository())),patch.object(fan_tables.TemperatureReader,'read',return_value={'SEN4':50}):
                controller=fan.Controller()
                state=controller.tick(0)
                self.assertEqual(state['policy_source'],'platform-acpr')
                self.assertEqual(state['requested'],70)
                with patch.object(fan_tables.TemperatureReader,'read',return_value={'SEN4':65}):
                    self.assertEqual(controller.tick(0)['requested'],100)
                config.write_text(json.dumps({'mode':'manual','percent':40}))
                self.assertEqual(controller.tick(0)['requested'],40)


if __name__ == '__main__':
    unittest.main()
