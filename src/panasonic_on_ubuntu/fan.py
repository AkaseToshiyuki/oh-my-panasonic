"""Temperature-based fan control using the selected driver's expiring lease."""
import json
import math
from pathlib import Path
import time

CONFIG = Path('/etc/panasonic-on-ubuntu/fan-control.json')
FIRMWARE = Path('/sys/devices/platform/pou-firmware')
TEMPS = (35, 50, 65, 75, 85)
PERFORMANCE = (52, 64, 80, 92, 100)
QUIET = (20, 30, 50, 76, 100)
DEFAULT = dict(mode='follow', percent=60, response=6, curve=[40, 50, 66, 82, 100])
MODES = ('follow', 'standard', 'quiet', 'performance', 'custom', 'manual')


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

    def release(self):
        if self.engaged:
            (FIRMWARE / 'fan_pwm').write_text('0')
            self.engaged = False
        self.filtered = None

    def restore(self, base_profile):
        self.release()
        if self.profile_owned:
            (FIRMWARE / 'fan_profile').write_text(str(base_profile))
            self.profile_owned = False

    def tick(self, base_profile):
        config = configuration()
        mode = config['mode']
        if mode != self.mode:
            self.filtered = None
        self.mode = mode
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
        quiet_curve = software_quiet and (mode == 'quiet' or mode == 'follow' and base_profile == 1)
        if mode == 'follow' and not quiet_curve:
            self.restore(base_profile)
            return state
        profile = 1 if mode == 'quiet' or quiet_curve else 0
        if int((FIRMWARE / 'fan_profile').read_text()) != profile:
            (FIRMWARE / 'fan_profile').write_text(str(profile))
        self.profile_owned = True
        if mode in ('quiet', 'standard') and not quiet_curve:
            self.release()
            return state
        temp = int((FIRMWARE / 'package_temperature_c').read_text())
        now = time.monotonic()
        if self.filtered is None or now - self.stamp > 8:
            self.filtered = temp
        else:
            self.filtered += (temp - self.filtered) * (1 - math.exp(-(now - self.stamp) / config['response']))
        self.stamp = now
        curve = QUIET if quiet_curve else PERFORMANCE if mode == 'performance' else config['curve']
        level = config['percent'] if mode == 'manual' else interpolate(self.filtered, curve)
        if temp >= 82:
            level = 100
        percent = max(20, min(100, round(level / 2) * 2))
        self.engaged = True
        (FIRMWARE / 'fan_pwm').write_text(str(percent))
        state['requested'] = percent
        return state
