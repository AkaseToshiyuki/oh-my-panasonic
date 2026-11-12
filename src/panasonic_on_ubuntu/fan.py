"""Temperature-based fan control using the selected driver's expiring lease."""
import json
import math
from pathlib import Path
import time
from . import fan_tables

CONFIG = Path('/etc/panasonic-on-ubuntu/fan-control.json')
FIRMWARE = Path('/sys/devices/platform/pou-firmware')
TEMPS = (35, 50, 65, 75, 85)
PERFORMANCE = (52, 64, 80, 92, 100)
QUIET = (20, 30, 50, 76, 100)
DEFAULT = dict(mode='follow', percent=60, response=6, curve=[40, 50, 66, 82, 100])
MODES = ('follow', 'standard', 'quiet', 'performance', 'custom', 'manual')
CPU_GUARD = ((35, 20), (70, 20), (75, 50), (82, 100))


def effective_mode(mode, base_profile):
    return ('quiet' if base_profile == 1 else 'standard') if mode == 'follow' else mode


def plot(config, base_profile, oem=None, software_quiet=False):
    mode = effective_mode(config['mode'], base_profile)
    series = []
    preset = {'standard': 'normal', 'quiet': 'slow', 'performance': 'fast'}.get(mode)
    if oem and preset:
        series = [{'label': fan_tables.SENSORS[name], 'points': points}
                  for name, points in oem[preset].items()]
        points = [(t,v) for t,v in zip(TEMPS,PERFORMANCE) if t < 82] + [(82,100),(85,100)] if mode == 'performance' else CPU_GUARD
        series.append({'label': 'CPU', 'points': list(points)})
    elif mode in ('custom', 'performance') or mode == 'quiet' and software_quiet:
        values = config['curve'] if mode == 'custom' else PERFORMANCE if mode == 'performance' else QUIET
        points = list(zip(TEMPS, values))
        # The control service overrides the smoothed curve at this temperature.
        points = [p for p in points if p[0] < 82] + [(82, 100), (85, 100)]
        series = [{'label': 'CPU', 'points': points}]
    elif mode == 'manual':
        series = [{'label': 'CPU', 'points': [(35, config['percent']), (81.9, config['percent']), (82, 100), (85, 100)]}]
    return {'mode': mode, 'editable': mode == 'custom', 'series': series,
            'firmware': not bool(series)}


def validate(config):
    data = {**DEFAULT, **config}
    if data['mode'] not in MODES:
        raise ValueError('风扇模式无效')
    for name, low, high in [('percent', 20, 100), ('response', 1, 20)]:
        if type(data[name]) is not int or not low <= data[name] <= high:
            raise ValueError('风扇设置超出范围')
    curve = data['curve']
    if (not isinstance(curve, list) or len(curve) != len(TEMPS)
            or any(type(v) is not int or not 20 <= v <= 100 for v in curve)
            or sorted(curve) != curve or curve[-1] != 100):
        raise ValueError('曲线须随温度持平或上升，85 °C 时达到 100%')
    return data


def configuration():
    try:
        return validate(json.loads(CONFIG.read_text()))
    except FileNotFoundError:
        return dict(DEFAULT)


def interpolate(temp, values):
    for index in range(1, len(TEMPS)):
        if temp <= TEMPS[index]:
            low = index - 1
            fraction = max(0, (temp - TEMPS[low]) / (TEMPS[index] - TEMPS[low]))
            return values[low] + fraction * (values[index] - values[low])
    return values[-1]


class Controller:
    def __init__(self):
        self.filtered = self.stamp = self.mode = None
        self.engaged = False
        self.profile_owned = False
        self.oem = fan_tables.load()
        self.sensor_filtered = {}

    def release(self):
        if self.engaged:
            (FIRMWARE / 'fan_pwm').write_text('0')
            self.engaged = False
        self.filtered = None
        self.sensor_filtered.clear()

    def restore(self, base_profile):
        self.release()
        if self.profile_owned:
            (FIRMWARE / 'fan_profile').write_text(str(base_profile))
            self.profile_owned = False

    def tick(self, base_profile):
        config = configuration()
        mode = config['mode']
        resolved = effective_mode(mode, base_profile)
        if resolved != self.mode:
            self.filtered = None
            self.sensor_filtered.clear()
        self.mode = resolved
        state = {**config, 'available': False, 'error': ''}
        try:
            state['feedback'] = int((FIRMWARE / 'fan_feedback_percent').read_text())
            state['available'] = True
        except (OSError, ValueError):
            self.restore(base_profile)
            return state
        try:
            software_quiet = (FIRMWARE / 'software_quiet_curve').read_text().strip() == '1'
        except OSError:
            software_quiet = False
        oem = self.oem if software_quiet else None
        state['plot'] = plot(config, base_profile, oem, software_quiet)
        preset = {'standard': 'normal', 'quiet': 'slow', 'performance': 'fast'}.get(resolved)
        oem_active = bool(oem and preset)
        quiet_curve = software_quiet and (mode == 'quiet' or mode == 'follow' and base_profile == 1)
        if mode == 'follow' and not quiet_curve and not oem_active:
            self.restore(base_profile)
            return state
        profile = 1 if resolved == 'quiet' else 0
        if int((FIRMWARE / 'fan_profile').read_text()) != profile:
            (FIRMWARE / 'fan_profile').write_text(str(profile))
        self.profile_owned = True
        if mode in ('quiet', 'standard') and not quiet_curve and not oem_active:
            self.release()
            return state
        temp = int((FIRMWARE / 'package_temperature_c').read_text())
        now = time.monotonic()
        fraction = 1 if self.filtered is None or now - self.stamp > 8 else 1 - math.exp(-(now - self.stamp) / config['response'])
        if self.filtered is None or now - self.stamp > 8:
            self.filtered = temp
        else:
            self.filtered += (temp - self.filtered) * (1 - math.exp(-(now - self.stamp) / config['response']))
        self.stamp = now
        curve = QUIET if quiet_curve else PERFORMANCE if mode == 'performance' else config['curve']
        level = config['percent'] if mode == 'manual' else interpolate(self.filtered, curve)
        if oem_active:
            sensors = fan_tables.temperatures(oem[preset])
            state['temperatures'] = sensors
            state['policy_source'] = 'platform-acpr'
            level = interpolate(self.filtered, PERFORMANCE) if resolved == 'performance' else fan_tables.interpolate(self.filtered, CPU_GUARD)
            for name, actual in sensors.items():
                previous = self.sensor_filtered.get(name, actual)
                filtered = previous + (actual - previous) * fraction
                self.sensor_filtered[name] = filtered
                points = oem[preset][name]
                level = max(level, fan_tables.interpolate(filtered, points))
                if points[-1][1] == 100 and actual >= points[-1][0]:
                    level = 100
        if temp >= 82:
            level = 100
        percent = max(20, min(100, (math.ceil(level / 2) if oem_active else round(level / 2)) * 2))
        self.engaged = True
        (FIRMWARE / 'fan_pwm').write_text(str(percent))
        state['requested'] = percent
        return state
