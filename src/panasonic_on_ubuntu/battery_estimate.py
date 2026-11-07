"""Short-window battery estimates, reset at power transitions and sample gaps."""
import math
from statistics import median


def identity(sample):
    packs = sample.get('batteries') or []
    if len(packs) != 1:
        return None
    pack = packs[0]
    fields = pack.get('fields', {})
    status = fields.get('status', {}).get('value')
    if status not in ('Charging', 'Discharging'):
        return None
    return (pack.get('name'), fields.get('model_name', {}).get('value'), status,
            sample.get('power_input', {}).get('source'))


def rates(current, samples):
    key = identity(current)
    if key is None:
        return {}
    now = current['timestamp']
    window = [current]
    previous = now
    for sample in sorted(samples, key=lambda s: s.get('timestamp', 0), reverse=True):
        stamp = sample.get('timestamp', 0)
        if stamp >= previous:
            continue
        if stamp < now-300 or previous-stamp > 90 or identity(sample) != key:
            break
        window.append(sample)
        previous = stamp
    if len(window) < 3 or now-previous < 60:
        return {}
    result = {}
    for field in ('power_now', 'current_now'):
        values = []
        for sample in window:
            try:
                value = abs(float(sample['batteries'][0]['fields'][field]['value']))
            except (KeyError, ValueError, TypeError):
                values = []
                break
            if not math.isfinite(value) or value <= 0:
                values = []
                break
            values.append(value)
        if values:
            result[field] = median(values)
    return result
