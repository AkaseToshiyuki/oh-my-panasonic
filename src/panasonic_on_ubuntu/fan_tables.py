"""Read the platform's active-cooling points without distributing firmware data."""
import hashlib
import lzma
from pathlib import Path
import struct

VAULT = Path('/sys/devices/platform/INTC1041:00/data_vault')
THERMAL = Path('/sys/class/thermal')
SENSORS = {'SEN3': '机身', 'SEN4': '供电电路', 'SEN7': '充电器'}
LIMIT = 512 * 1024


def segments(blob):
    if len(blob) > LIMIT:
        raise ValueError('Thermal repository too large')
    offset = 0
    while offset < len(blob):
        if len(blob) - offset < 148:
            raise ValueError('Truncated thermal header')
        signature, size, version, flags = struct.unpack_from('<HHII', blob, offset)
        length, kind = struct.unpack_from('<I4s', blob, offset + 140)
        if signature != 0x1fe5 or size != 148 or version != 0x02000000:
            raise ValueError('Unknown thermal repository')
        end = offset + size + length
        if end > len(blob):
            raise ValueError('Truncated thermal payload')
        payload = blob[offset + size:end]
        if hashlib.sha256(payload).digest() != blob[offset + 108:offset + 140]:
            raise ValueError('Thermal payload checksum mismatch')
        if flags & 0x40000000:
            if len(payload) < 13 or not 0 < struct.unpack_from('<Q', payload, 5)[0] <= LIMIT:
                raise ValueError('Invalid decompressed length')
            expected = struct.unpack_from('<Q', payload, 5)[0]
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE, memlimit=64 * 1024 * 1024)
            payload = decoder.decompress(payload, max_length=expected + 1)
            if len(payload) != expected:
                raise ValueError('Truncated compressed repository')
        yield kind, payload
        offset = end


def entries(blob, depth=0):
    if depth > 2:
        raise ValueError('Nested thermal repository')
    for kind, payload in segments(blob):
        if kind == b'REPO':
            yield from entries(payload, depth + 1)
            continue
        if kind != b'KEYS':
            raise ValueError('Unknown thermal payload')
        p = 0
        while p < len(payload):
            if len(payload) - p < 10:
                raise ValueError('Truncated thermal key')
            sig, flags, length = struct.unpack_from('<HII', payload, p)
            p += 10
            if sig != 0xa0d8 or not 1 < length <= 1024 or p + length + 8 > len(payload):
                raise ValueError('Invalid thermal key')
            name = payload[p:p + length]
            p += length
            typ, count = struct.unpack_from('<II', payload, p)
            p += 8
            if not name.endswith(b'\0') or p + count > len(payload):
                raise ValueError('Invalid thermal entry')
            yield name[:-1].decode('ascii'), payload[p:p + count]
            p += count


def fields(blob):
    values = []
    p = 0
    while p < len(blob):
        if len(blob) - p < 12:
            raise ValueError('Truncated active-cooling field')
        typ, value = struct.unpack_from('<IQ', blob, p)
        p += 12
        if typ == 8:
            if not 0 < value <= 128 or p + value > len(blob):
                raise ValueError('Invalid active-cooling string')
            value, p = blob[p:p + value].rstrip(b'\0').decode('ascii'), p + value
        elif typ != 4:
            raise ValueError('Unknown active-cooling field type')
        values.append(value)
    return values


def parse(blob):
    tables = {}
    for key, value in entries(blob):
        if key not in [f'/shared/tables/acpr/fan_{name}' for name in ('normal', 'slow', 'fast')]:
            continue
        data = fields(value)
        if data[:2] != [1, 1] or (len(data) - 2) % 7:
            raise ValueError('Unknown active-cooling table layout')
        curves = {}
        for start in range(2, len(data), 7):
            source, temperature, target, control, percent, interpolation, response = data[start:start + 7]
            sensor = source.rsplit('.', 1)[-1]
            if (sensor not in SENSORS or source != '\\_SB_.PC00.LPCB.EC0_.' + sensor
                    or target != '\\_SB_.PC00.LPCB.EC0_.TFN1' or control != 393216
                    or interpolation != 1 or not 1 <= response <= 100):
                raise ValueError('Unsupported active-cooling relationship')
            celsius = (temperature - 2732) / 10
            if not 10 <= celsius <= 100 or not 0 <= percent <= 100:
                raise ValueError('Active-cooling point out of bounds')
            points = curves.setdefault(sensor, [])
            if points and (points[-1][0] >= celsius or points[-1][1] > percent):
                raise ValueError('Non-monotonic active-cooling curve')
            points.append((celsius, percent))
        if not curves or any(len(p) < 2 for p in curves.values()):
            raise ValueError('Incomplete active-cooling curve')
        tables[key.rsplit('_', 1)[-1]] = curves
    if set(tables) != {'normal', 'slow', 'fast'}:
        raise ValueError('Missing active-cooling presets')
    return tables


def load():
    from .hardware import selection
    if selection()[0] != 'sr3':
        return None
    try:
        with VAULT.open('rb') as stream:
            return parse(stream.read(LIMIT + 1))
    except (OSError, ValueError, lzma.LZMAError, struct.error, UnicodeError):
        return None


def temperatures(names):
    result = {}
    for zone in THERMAL.glob('thermal_zone*'):
        name = (zone / 'type').read_text().strip()
        if name in names:
            value = int((zone / 'temp').read_text()) / 1000
            if not 0 < value < 110 or name in result:
                raise ValueError('Invalid platform temperature')
            result[name] = value
    if set(result) != set(names):
        raise ValueError('Platform temperature unavailable')
    return result


def interpolate(temp, points, minimum=20):
    if temp < points[0][0]:
        return minimum
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if temp <= t1:
            return v0 + (v1 - v0) * (temp - t0) / (t1 - t0)
    return points[-1][1]
