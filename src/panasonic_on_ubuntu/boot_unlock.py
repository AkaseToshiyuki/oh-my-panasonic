"""Validated boot transaction records for optional SR3 power-limit control."""
from dataclasses import dataclass
from pathlib import Path
import enum
import struct
import zlib

CPU_SIZE = 961
STATE = struct.Struct('<8sIIII32s32sI')
BACKUP = struct.Struct('<8sII32s32s961sI')
STATE_MAGIC = b'OMP35S1\0'
BACKUP_MAGIC = b'OMP35B1\0'
OWNED = (*range(0x17, 0x1c), 0x227)


class Phase(enum.IntEnum):
    ENABLE = 1
    APPLY_PENDING = 2
    BOOT_PENDING = 3
    ACTIVE = 4
    DISABLED = 5
    RESTORE = 6
    ERROR = 7


def text_field(value):
    raw = value.encode('ascii')
    if len(raw) >= 32 or b'\0' in raw:
        raise ValueError('Invalid identity string')
    return raw.ljust(32, b'\0')


def parse_text(raw):
    value, separator, padding = raw.partition(b'\0')
    if not separator or padding.strip(b'\0'):
        raise ValueError('Invalid identity encoding')
    return value.decode('ascii')


def supported(sku, bios):
    return sku == 'CF-SR3SKAKS' and bios in ('V1.00L12', 'V1.00L27')


def crc(data):
    return zlib.crc32(data) & 0xffffffff


def checked_unpack(layout, data, magic):
    if len(data) != layout.size:
        raise ValueError('Incorrect boot record size')
    fields = layout.unpack(data)
    if fields[0] != magic or fields[1] != 1 or fields[-1] != crc(data[:-4]):
        raise ValueError('Corrupt or unsupported boot record')
    return fields


def finish_record(layout, *fields):
    data = layout.pack(*fields, 0)
    return data[:-4] + struct.pack('<I', crc(data[:-4]))


def stock_cpu(cpu):
    if len(cpu) != CPU_SIZE:
        return False
    u32 = lambda offset: struct.unpack_from('<I', cpu, offset)[0]
    return (cpu[0x30] == 1 and u32(0x17) == 0 and cpu[0x1b] == 0
            and cpu[0x227] == 1 and all(u32(offset) == 28000 and u32(offset + 4) == 55000
                                      for offset in (0x5b, 0x65, 0x6f)))


def candidate(cpu):
    if not stock_cpu(cpu):
        raise ValueError('Original CPU configuration is outside the validated profile')
    result = bytearray(cpu)
    struct.pack_into('<I', result, 0x17, 35000)
    result[0x1b] = 1
    result[0x227] = 0
    return bytes(result)


def restore_owned(current, original):
    expected = candidate(original)
    if len(current) != CPU_SIZE or current[0x30] != 1:
        raise ValueError('Current CPU configuration is not supported')
    if any(current[i] not in (expected[i], original[i]) for i in OWNED):
        raise ValueError('Power settings changed outside this transaction')
    result = bytearray(current)
    for i in OWNED:
        result[i] = original[i]
    return bytes(result)


@dataclass(frozen=True)
class Backup:
    sku: str
    bios: str
    cpu: bytes

    def encode(self):
        if not supported(self.sku, self.bios) or not stock_cpu(self.cpu):
            raise ValueError('Unsupported backup configuration')
        return finish_record(BACKUP, BACKUP_MAGIC, 1, CPU_SIZE,
                             text_field(self.sku), text_field(self.bios), self.cpu)

    @property
    def checksum(self):
        return struct.unpack('<I', self.encode()[-4:])[0]

    @classmethod
    def decode(cls, data):
        _, _, size, sku, bios, cpu, _ = checked_unpack(BACKUP, data, BACKUP_MAGIC)
        result = cls(parse_text(sku), parse_text(bios), cpu)
        if size != CPU_SIZE or result.encode() != data:
            raise ValueError('Backup is not a validated original')
        return result


@dataclass(frozen=True)
class BootState:
    phase: Phase
    generation: int
    backup_crc: int
    sku: str
    bios: str

    def encode(self):
        if not supported(self.sku, self.bios):
            raise ValueError('Unsupported boot identity')
        return finish_record(STATE, STATE_MAGIC, 1, int(Phase(self.phase)), self.generation,
                             self.backup_crc, text_field(self.sku), text_field(self.bios))

    @classmethod
    def decode(cls, data):
        _, _, phase, generation, backup_crc, sku, bios, _ = checked_unpack(STATE, data, STATE_MAGIC)
        result = cls(Phase(phase), generation, backup_crc, parse_text(sku), parse_text(bios))
        if result.encode() != data:
            raise ValueError('Invalid boot state')
        return result

    def validate_backup(self, backup):
        if (self.sku, self.bios, self.backup_crc) != (backup.sku, backup.bios, backup.checksum):
            raise ValueError('Boot state and original backup do not match')


def load_records(directory):
    directory = Path(directory)
    state = BootState.decode((directory / 'state.bin').read_bytes())
    backup = Backup.decode((directory / 'original.bin').read_bytes())
    state.validate_backup(backup)
    return state, backup
