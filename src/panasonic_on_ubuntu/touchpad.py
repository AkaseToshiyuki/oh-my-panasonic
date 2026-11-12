"""Device identities and geometry for supported circular touchpads."""
from pathlib import Path
import json
import re

PROFILES = {
    'CFSV7-3L': ('SynPS/2 Synaptics TouchPad', (0x11, 2, 7), 1830.0, 1.332361516, 500.0),
    'CFSR3-1': ('SYNA0103:00 06CB:CE53 Touchpad', (0x18, 0x06cb, 0xce53), 308.4, 1.0, 84.4),
}


def model():
    return Path('/sys/class/dmi/id/product_name').read_text().strip()


def validate(device, machine=None):
    machine = model() if machine is None else machine
    profile = PROFILES.get(machine)
    if not profile or device.name != profile[0] or tuple(device.info[:3]) != profile[1]:
        raise ValueError('触控板与当前机型不匹配')
    if machine == 'CFSR3-1':
        from evdev import ecodes
        for axis in (ecodes.ABS_X, ecodes.ABS_Y, ecodes.ABS_MT_POSITION_X, ecodes.ABS_MT_POSITION_Y):
            info = device.absinfo(axis)
            if info is None or (info.min, info.max, info.resolution) != (0, 771, 12):
                raise ValueError('触控板坐标范围不匹配')
    return profile


def configuration(sensitivity, natural, machine=None):
    if type(sensitivity) is not int or sensitivity not in range(-2, 3) or natural not in (0, 1):
        raise ValueError('Invalid touchpad setting')
    profile = PROFILES.get(model() if machine is None else machine)
    if not profile:
        raise ValueError('本机尚不支持圆形滚动')
    name, _, radius, scale, minimum = profile
    pattern = '^' + re.escape(name).replace(r'\ ', ' ') + '$'
    return f'''device_name_regex = {json.dumps(pattern)}
[scroll]
enable = true
reverse_vertical = {str(bool(natural)).lower()}
horizontal_enable = false
sensitivity = {sensitivity}
detect_area_width = 0
detect_area_radius = {radius}
coordinate_y_scale = {scale}
minimum_rotation_radius = {minimum}
[log]
level = "info"
'''
