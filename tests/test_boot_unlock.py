import dataclasses
import struct
import unittest
from panasonic_on_ubuntu.boot_unlock import Backup, BootState, Phase, candidate, restore_owned


def original():
    value=bytearray(961)
    value[0x30]=1;value[0x227]=1
    for offset in (0x5b,0x65,0x6f):
        struct.pack_into('<II',value,offset,28000,55000)
    return bytes(value)


class BootRecordsTests(unittest.TestCase):
    def test_binary_round_trip(self):
        backup=Backup('CF-SR3SKAKS','V1.00L27',original())
        self.assertEqual(Backup.decode(backup.encode()),backup)
        for phase in Phase:
            state=BootState(phase,2,backup.checksum,backup.sku,backup.bios)
            self.assertEqual(BootState.decode(state.encode()),state)
            state.validate_backup(backup)

    def test_damage_and_wrong_size_rejected(self):
        backup=Backup('CF-SR3SKAKS','V1.00L27',original()).encode()
        for value in (backup[:-1],backup+b'\0',backup[:80]+bytes([backup[80]^1])+backup[81:]):
            with self.assertRaises(ValueError):Backup.decode(value)

    def test_mixed_backup_rejected(self):
        backup=Backup('CF-SR3SKAKS','V1.00L27',original())
        state=BootState(Phase.ACTIVE,1,backup.checksum,backup.sku,'V1.00L12')
        with self.assertRaises(ValueError):state.validate_backup(backup)

    def test_only_owned_fields_change(self):
        stock=original();changed=candidate(stock)
        self.assertEqual({i for i,(a,b) in enumerate(zip(stock,changed)) if a!=b},{0x17,0x18,0x1b,0x227})
        changed=bytearray(changed);changed[0x100]=42
        restored=restore_owned(changed,stock)
        self.assertEqual(restored[0x100],42)
        self.assertEqual(restored[:0x100],stock[:0x100])

    def test_external_power_change_rejected(self):
        stock=original();changed=bytearray(candidate(stock))
        struct.pack_into('<I',changed,0x17,40000)
        with self.assertRaises(ValueError):restore_owned(changed,stock)

    def test_unlocked_or_unknown_firmware_rejected(self):
        stock=bytearray(original());stock[0x30]=0
        with self.assertRaises(ValueError):candidate(stock)
        with self.assertRaises(ValueError):Backup('CF-SR3SKAKS','V1.00L28',original()).encode()


if __name__=='__main__':unittest.main()
