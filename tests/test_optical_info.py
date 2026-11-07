import unittest
from unittest.mock import patch
from panasonic_on_ubuntu.optical_info import describe,parse,collect


class MediaTests(unittest.TestCase):
    def test_capabilities_are_distinct_from_inserted_media(self):
        props={'ID_CDROM_CD_RW':'1','ID_CDROM_DVD_RW':'1','ID_CDROM_MEDIA':'1','ID_CDROM_MEDIA_CD':'1'}
        value=describe(props,{'MediaAvailable':True,'Size':700000000},{})
        self.assertEqual(value['capabilities'],'CD-RW / DVD-RW')
        self.assertEqual(value['attributes'],'CD-ROM')
        self.assertIn('只读',value['access']);self.assertNotIn('lifetime',value)
        self.assertIsNone(value['used'])
    def test_blank_disc_not_given_a_fictitious_nominal_capacity(self):
        props={'ID_CDROM_DVD_PLUS_R':'1','ID_CDROM_MEDIA':'1','ID_CDROM_MEDIA_DVD_PLUS_R':'1','ID_CDROM_MEDIA_STATE':'blank'}
        value=describe(props,{'Size':0},{'Size':0})
        self.assertEqual(value['used'],'0 B');self.assertIsNone(value['capacity'])
        self.assertIn('可刻录',value['access'])
    def test_finalized_recordable_disc_cannot_be_appended(self):
        props={'ID_CDROM_CD_R':'1','ID_CDROM_MEDIA':'1','ID_CDROM_MEDIA_CD_R':'1','ID_CDROM_MEDIA_STATE':'complete'}
        value=describe(props,{},{});self.assertIn('已封盘',value['access']);self.assertNotIn('可刻录',value['access'])
    def test_rewritable_disc_and_mounted_used_space(self):
        props={'ID_CDROM_BD_RE':'1','ID_CDROM_MEDIA':'1','ID_CDROM_MEDIA_BD_RE':'1'}
        value=describe(props,{'Size':25000000000},{'IdType':'udf'},{'total':24000000000,'used':5000000000})
        self.assertIn('可擦写',value['access']);self.assertEqual(value['used'],'5.00 GB');self.assertEqual(value['capacity'],'25.00 GB')
    def test_no_disc_hides_stale_capacity_and_serials_are_not_cached(self):
        value=describe({}, {'MediaAvailable':False,'Size':700000000},{'IdType':'iso9660'})
        self.assertEqual(value['media'],'无光盘');self.assertIsNone(value['capacity'])
        self.assertEqual(parse('E:ID_SERIAL=private\nID_FS_LABEL=private\nID_CDROM_DVD=1'),{'ID_CDROM_DVD':'1'})
    def test_powered_off_never_opens_device_or_queries_bus(self):
        with patch('panasonic_on_ubuntu.optical_info.Gio.bus_get_sync') as bus,patch('panasonic_on_ubuntu.optical_info.subprocess.run') as command:
            value=collect({'model':'fixture','block_present':False,'powered':False})
        bus.assert_not_called();command.assert_not_called()
        self.assertIsNone(value['media'])
