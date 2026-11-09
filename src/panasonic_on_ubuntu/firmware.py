"""Read only the model-scoped driver's public attributes."""
from pathlib import Path

ROOT = Path('/sys/devices/platform/pou-firmware')


def fan_rpm(hwmon=Path('/sys/class/hwmon')):
    for sensor in hwmon.glob('hwmon*'):
        for identity in ('device/firmware_node/path', 'device/path'):
            try:
                path = (sensor / identity).read_text().strip().replace('_', '')
                if path not in (r'\SB.PCI0.LPCB.EC0.TFN1', r'\TZ.FAN0'):
                    continue
                rpm = int((sensor / 'fan1_input').read_text())
                if 0 <= rpm < 65535:
                    return rpm
            except (OSError, ValueError):
                continue
    return None


def status(names=None):
    result = {'fan_rpm': fan_rpm()}
    for name in names if names is not None else ('fan_profile', 'original_fan_profile', 'eco_raw', 'power_limits_enabled', 'optical_power', 'optical_capabilities', 'ec_pl1_raw', 'ec_hot_pl1_raw', 'ec_pl2_raw', 'ec_trip_raw', 'manual_fan_supported_raw', 'manual_fan_value_raw', 'performance_lease', 'performance_lease_version', 'package_temperature_c'):
        try:
            result[name] = int((ROOT / name).read_text().strip())
        except (OSError, ValueError):
            result[name] = None
    return result


def fan_label(value):
    return {0: '标准自动曲线', 1: '静音自动曲线'}.get(value, '固件模块不可用')
