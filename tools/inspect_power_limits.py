"""Read-only Intel power-limit evidence and before/after comparison.

No argument performs hardware access. --run reads existing Linux interfaces;
it never loads drivers, writes registers, changes firmware or starts a load.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
import time

SCHEMA = 1
TEMPERATURE_CUTOFF_C = 85
CPU_GUID = 'b08f97ff-e6e8-4193-a997-5e9e9b0adb32'
SETUP_GUID = 'ec87d643-eba4-4bb5-a1e5-3f3e36b20da9'
REGISTERS = (0x606, 0x610, 0x614)
SETTINGS = ('power.json', 'fan.json', 'battery-care.json')


def uint64(value):
    if isinstance(value, str):
        value = int(value, 0)
    if type(value) is not int or not 0 <= value < 1 << 64:
        raise ValueError('MSR value must be an unsigned 64-bit integer')
    return value


def decode_msrs(raw):
    unit = uint64(raw['0x606'])
    limit = uint64(raw['0x610'])
    sku = uint64(raw['0x614'])
    power_unit = 2.0 ** -(unit & 15)
    time_unit = 2.0 ** -((unit >> 16) & 15)

    def window(code):
        return (2 ** (code & 31)) * (1 + ((code >> 5) & 3) / 4) * time_unit

    def constraint(shift):
        bits = limit >> shift
        return {'watts': (bits & 0x7fff) * power_unit,
                'enabled': bool(bits & (1 << 15)), 'clamp': bool(bits & (1 << 16)),
                'window_seconds': window((bits >> 17) & 127)}

    return {'power_unit_w': power_unit, 'time_unit_s': time_unit,
            'pl1': constraint(0), 'pl2': constraint(32), 'locked': bool(limit >> 63),
            'sku_thermal_w': (sku & 0x7fff) * power_unit,
            'sku_min_w': ((sku >> 16) & 0x7fff) * power_unit or None,
            'sku_max_w': ((sku >> 32) & 0x7fff) * power_unit or None}


def read_msrs(path):
    result = {}
    with path.open('rb', buffering=0) as device:
        for register in REGISTERS:
            device.seek(register)
            data = device.read(8)
            if len(data) != 8:
                raise OSError(f'Short MSR read at {register:#x}')
            result[hex(register)] = f'0x{int.from_bytes(data, "little"):016x}'
    return result


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_small(path):
    with path.open('rb') as source:
        data = source.read(65537)
    if len(data) > 65536:
        raise ValueError('Input exceeds 64 KiB')
    return data


def collect(root=Path('/')):
    errors = []

    def get(relative, binary=False, optional=False):
        try:
            value = read_small(root / relative)
            return value if binary else value.decode('utf-8').strip()
        except (OSError, UnicodeError, ValueError) as exc:
            if not optional or not isinstance(exc, FileNotFoundError):
                errors.append({'path': relative, 'error': type(exc).__name__})
            return None

    dmi = {key: get('sys/class/dmi/id/' + key) for key in
           ('sys_vendor', 'product_name', 'product_sku', 'bios_version')}
    cpu = {}
    first_cpu = (get('proc/cpuinfo') or '').split('\n\n', 1)[0]
    for line in first_cpu.splitlines():
        key, sep, value = line.partition(':')
        if sep and key.strip() in ('vendor_id', 'cpu family', 'model', 'model name'):
            cpu[key.strip()] = value.strip()
    raw = {}
    # These CPU models are used by the validated SV7 and SR3. Other systems
    # still get sysfs evidence without speculative register access.
    if (cpu.get('vendor_id') == 'GenuineIntel' and cpu.get('cpu family') == '6'
            and cpu.get('model') in ('142', '154')):
        try:
            raw = read_msrs(root / 'dev/cpu/0/msr')
        except (OSError, ValueError) as exc:
            errors.append({'path': 'dev/cpu/0/msr', 'error': type(exc).__name__})
    firmware = {}
    for name, guid in [('CpuSetup', CPU_GUID), ('Setup', SETUP_GUID)]:
        value = get(f'sys/firmware/efi/efivars/{name}-{guid}', binary=True, optional=True)
        if value is None:
            firmware[name] = {'available': False}
        elif len(value) < 4:
            errors.append({'path': name, 'error': 'ShortEfiVariable'})
            firmware[name] = {'available': False}
        else:
            firmware[name] = {'available': True, 'attributes': int.from_bytes(value[:4], 'little'),
                              'size': len(value) - 4, 'sha256': digest(value),
                              'layout_verified_on_device': False}
    settings = {}
    for name in SETTINGS:
        relative = 'etc/panasonic-on-ubuntu/' + name
        value = get(relative, binary=True, optional=True)
        settings[name] = {'present': value is not None,
                          'sha256': digest(value) if value is not None else None}
    rapl = {}
    for zone in ('intel-rapl:0', 'intel-rapl-mmio:0'):
        values = {}
        for key in ('name', 'enabled', 'constraint_0_name', 'constraint_1_name',
                    'constraint_0_power_limit_uw', 'constraint_1_power_limit_uw',
                    'constraint_0_time_window_us', 'constraint_1_time_window_us'):
            value = get(f'sys/class/powercap/{zone}/{key}', optional=True)
            if value is not None:
                values[key] = value
        if values:
            rapl[zone] = values
    temperatures = []
    for hwmon in sorted((root / 'sys/class/hwmon').glob('hwmon*')):
        if get(str(hwmon.relative_to(root) / 'name'), optional=True) != 'coretemp':
            continue
        for sensor in sorted(hwmon.glob('temp*_input')):
            value = get(str(sensor.relative_to(root)), optional=True)
            try:
                temperature = float(value) / 1000
                if not math.isfinite(temperature) or not -20 <= temperature <= 130:
                    raise ValueError('Out of range')
                temperatures.append(temperature)
            except (ValueError, TypeError):
                errors.append({'path': str(sensor.relative_to(root)), 'error': 'InvalidTemperature'})
    return {'schema': SCHEMA, 'collected_at': time.time(), 'read_only': True,
            'boot_id': get('proc/sys/kernel/random/boot_id'), 'dmi': dmi, 'cpu': cpu,
            'raw_msrs': raw, 'decoded_msrs': decode_msrs(raw) if raw else None,
            'firmware': firmware, 'settings': settings, 'rapl': rapl,
            'temperature_c': max(temperatures) if temperatures else None,
            'errors': errors}


def assessment(snapshot):
    reasons = []
    decoded = snapshot.get('decoded_msrs')
    if not decoded:
        reasons.append('msr_evidence_missing')
    elif decoded['locked']:
        reasons.append('package_msr_locked_until_reset')
    temperature = snapshot.get('temperature_c')
    if type(temperature) not in (int, float) or not math.isfinite(temperature):
        reasons.append('temperature_missing_or_invalid')
    elif temperature >= TEMPERATURE_CUTOFF_C:
        reasons.append('temperature_at_or_above_85c')
    reasons.append('boot_path_and_recovery_not_verified')
    return {'hardware_writes_permitted': False, 'cutoff_c': TEMPERATURE_CUTOFF_C,
            'reasons': reasons, 'note': 'Read-only evidence; does not implement an active thermal guard.'}


def compare(before, after):
    for snapshot in (before, after):
        if snapshot.get('schema') != SCHEMA or snapshot.get('read_only') is not True:
            raise ValueError('Unsupported snapshot format')
        if any(not isinstance(snapshot.get(key), dict) for key in
               ('dmi', 'cpu', 'settings', 'firmware', 'raw_msrs', 'rapl')):
            raise ValueError('Missing or malformed snapshot section')
        if not isinstance(snapshot.get('errors'), list):
            raise ValueError('Missing or malformed collection errors')
        for name in ('CpuSetup', 'Setup'):
            if not isinstance(snapshot['firmware'].get(name), dict):
                raise ValueError('Missing firmware metadata')
        if snapshot['raw_msrs']:
            if set(snapshot['raw_msrs']) != {hex(r) for r in REGISTERS}:
                raise ValueError('Incomplete MSR register set')
            decode_msrs(snapshot['raw_msrs'])
    differences = []
    for category in ('dmi', 'cpu', 'settings', 'firmware', 'raw_msrs', 'rapl'):
        if before.get(category) != after.get(category):
            differences.append(category)
    incomplete = any(not s.get('raw_msrs') or not s.get('boot_id') or s.get('errors')
                     or not all(s['dmi'].get(k) for k in ('sys_vendor', 'product_name', 'product_sku', 'bios_version'))
                     or not all(s['cpu'].get(k) for k in ('vendor_id', 'cpu family', 'model'))
                     or set(s['settings']) != set(SETTINGS)
                     or not isinstance(s['rapl'].get('intel-rapl-mmio:0'), dict)
                     or not all(s['rapl']['intel-rapl-mmio:0'].get(k) for k in
                                ('constraint_0_power_limit_uw', 'constraint_1_power_limit_uw'))
                     or not s.get('firmware', {}).get('CpuSetup', {}).get('available')
                     or not s.get('firmware', {}).get('Setup', {}).get('available')
                     for s in (before, after))
    return {'status': 'mismatch' if differences else ('incomplete' if incomplete else 'matched'),
            'differences': differences, 'evidence_incomplete': incomplete,
            'different_boot': bool(before.get('boot_id') and after.get('boot_id')
                                   and before['boot_id'] != after['boot_id']),
            'scope': 'Observed settings and register state only; not a boot-recovery guarantee.'}


def write_new(path, value):
    # Exclusive creation also rejects existing symlinks; never overwrite a
    # baseline, device node or firmware variable when run as root.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as target:
        json.dump(value, target, indent=2, ensure_ascii=False, allow_nan=False)
        target.write('\n')


def backup_files():
    return {**{name: 'etc/panasonic-on-ubuntu/' + name for name in SETTINGS},
            'CpuSetup.efivar': f'sys/firmware/efi/efivars/CpuSetup-{CPU_GUID}',
            'Setup.efivar': f'sys/firmware/efi/efivars/Setup-{SETUP_GUID}'}


def backup(directory, snapshot, root=Path('/')):
    directory.mkdir(mode=0o700)
    records = {}
    for name, relative in backup_files().items():
        category = snapshot['firmware' if name.endswith('.efivar') else 'settings']
        metadata = category[name.removesuffix('.efivar')]
        expected = metadata.get('sha256')
        try:
            value = read_small(root / relative)
        except FileNotFoundError:
            value = None
        if (digest(value) if value is not None else None) != expected:
            raise ValueError(f'{name} changed after collection; capture a fresh baseline')
        records[name] = {'present': value is not None, 'sha256': expected}
        if value is not None:
            fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as target:
                target.write(value)
    manifest = {'schema': SCHEMA, 'dmi': snapshot['dmi'], 'files': records,
                'scope': 'Configuration bytes only; no SPI image or boot-failure recovery guarantee.'}
    write_new(directory / 'manifest.json', manifest)
    return {'files': len(records), 'verified': verify_backup(directory)['status'] == 'verified'}


def verify_backup(directory):
    manifest = load_snapshot(directory / 'manifest.json')
    if (manifest.get('schema') != SCHEMA or not isinstance(manifest.get('files'), dict)
            or set(manifest['files']) != set(backup_files()) or not isinstance(manifest.get('scope'), str)):
        raise ValueError('Unsupported backup manifest')
    for name, record in manifest['files'].items():
        if not isinstance(record, dict):
            raise ValueError('Invalid backup record')
        path = directory / name
        if path.is_symlink():
            raise ValueError('Backup contains a symbolic link')
        if record.get('present') is True:
            if digest(read_small(path)) != record.get('sha256'):
                raise ValueError(f'Backup checksum mismatch: {name}')
        elif record.get('present') is not False or record.get('sha256') is not None or path.exists():
            raise ValueError(f'Backup presence mismatch: {name}')
    return {'status': 'verified', 'scope': manifest['scope']}


def load_snapshot(path):
    value = json.loads(read_small(path), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    if not isinstance(value, dict):
        raise ValueError('Snapshot must be an object')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true', help='Collect read-only evidence on Linux')
    action.add_argument('--compare', type=Path, nargs=2, metavar=('BEFORE', 'AFTER'), help='Compare saved evidence offline')
    action.add_argument('--verify-backup', type=Path, metavar='DIRECTORY', help='Verify a saved configuration backup offline')
    parser.add_argument('--backup-dir', type=Path, help='With --run, save configuration bytes in a new private directory')
    parser.add_argument('--output', type=Path, help='New private JSON file; existing files are never replaced')
    args = parser.parse_args(argv)
    if args.backup_dir and not args.run:
        parser.error('--backup-dir requires --run')
    if not args.run and not args.compare and not args.verify_backup:
        parser.print_help()
        return
    if args.run:
        if platform.system() != 'Linux':
            parser.error('--run requires Linux; use --compare for offline evidence')
        report = collect()
        report['assessment'] = assessment(report)
        if args.backup_dir:
            report['backup'] = backup(args.backup_dir, report)
    elif args.verify_backup:
        report = verify_backup(args.verify_backup)
    else:
        report = compare(*(load_snapshot(p) for p in args.compare))
    if args.output:
        write_new(args.output, report)
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    from validation_cli import checked
    checked(main)
