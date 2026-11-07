"""A disk sharing the AHCI controller must prevent any optical teardown."""
import tempfile
from pathlib import Path
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from panasonic_on_ubuntu import optical


class OpticalGuardTests(unittest.TestCase):
    def test_resume_retries_busy_device_then_stops_after_success(self):
        with patch.object(optical,'supported',return_value=True),patch.object(optical,'status',return_value={'policy':'auto','model':'TEST'}),patch.object(optical,'switch',side_effect=[RuntimeError('busy'),None]) as switch,patch.object(optical,'atomic'),patch.object(optical.time,'sleep'):
            optical.auto(attempts=5,delay=2)
        self.assertEqual(switch.call_count,2)

    def test_resume_retry_respects_new_keep_on_policy(self):
        states=[{'policy':'auto','model':'TEST'},{'policy':'auto','model':'TEST'},{'policy':'on','model':'TEST'}]
        with patch.object(optical,'supported',return_value=True),patch.object(optical,'status',side_effect=states),patch.object(optical,'switch',side_effect=RuntimeError('busy')) as switch,patch.object(optical,'atomic'),patch.object(optical.time,'sleep'):
            optical.auto(attempts=5,delay=2)
        switch.assert_called_once_with('off')

    def test_resume_retry_is_bounded_even_when_drive_remains_busy(self):
        with patch.object(optical,'supported',return_value=True),patch.object(optical,'status',return_value={'policy':'auto','model':'TEST'}),patch.object(optical,'switch',side_effect=RuntimeError('busy')) as switch,patch.object(optical,'atomic'),patch.object(optical.time,'sleep') as sleep:
            optical.auto(attempts=5,delay=2)
        self.assertEqual(switch.call_count,5)
        self.assertEqual(sleep.call_count,4)

    @contextmanager
    def optical_device(self,mounted=False,busy='0'):
        original_read=Path.read_text
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);block=root/'sr0';controller=root/'controller';driver=root/'ahci'
            (block/'device').mkdir(parents=True);controller.mkdir();driver.mkdir()
            for p,value in [(block/'device/type','5'),(block/'device/device_busy',busy),(block/'device/model','TEST DVD'),(block/'dev','11:0'),(controller/'remapped_nvme','0')]:p.write_text(value)
            (controller/'driver').symlink_to(driver,target_is_directory=True)
            def read(p,*args,**kwargs):
                if str(p)=='/proc/self/mountinfo':return '1 1 11:0 / /media/disc ro - iso9660 /dev/sr0 ro' if mounted else ''
                return original_read(p,*args,**kwargs)
            with patch.object(optical,'blocks',return_value=[block]),patch.object(optical,'CONTROLLER',controller),patch.object(optical,'DRIVER',driver),patch.object(Path,'read_text',autospec=True,side_effect=read):yield

    def test_mounted_disc_is_rejected_before_device_open(self):
        with self.optical_device(mounted=True),patch.object(optical.os,'open') as opened:
            with self.assertRaisesRegex(RuntimeError,'已挂载'):optical.preflight()
            opened.assert_not_called()

    def test_present_media_open_tray_and_unknown_status_are_rejected(self):
        for state in (0,2,3,4):
            with self.subTest(state=state),self.optical_device(),patch.object(optical.os,'open',return_value=99),patch.object(optical.os,'close') as closed,patch.object(optical.fcntl,'ioctl',return_value=state):
                with self.assertRaisesRegex(RuntimeError,'有光盘'):optical.preflight()
                closed.assert_called_once_with(99)

    def test_active_device_is_rejected_even_without_disc(self):
        with self.optical_device(busy='1'),patch.object(optical.os,'open',return_value=99),patch.object(optical.os,'close'),patch.object(optical.fcntl,'ioctl',return_value=1):
            with self.assertRaisesRegex(RuntimeError,'正在使用'):optical.preflight()

    def test_power_on_capability_survives_driver_unbind(self):
        values={'product_name':'CFSV7-3L','bios_version':'V3.11L22','optical_capabilities':'9','class':'0x010601','vendor':'0x8086'}
        with patch.object(optical,'read',side_effect=lambda path:values.get(path.name)):
            self.assertTrue(optical.supported())

    def test_non_optical_disk_is_never_detached(self):
        with tempfile.TemporaryDirectory() as directory:
            block=Path(directory)/'sda'
            (block/'device').mkdir(parents=True)
            (block/'device/type').write_text('0')
            with patch.object(optical,'blocks',return_value=[block]):
                with self.assertRaisesRegex(RuntimeError,'并非仅连接一个光驱'):
                    optical.preflight()

    def test_additional_storage_blocks_detach(self):
        with patch.object(optical,'blocks',return_value=[Path('/fixture/sr0'),Path('/fixture/sda')]):
            with self.assertRaisesRegex(RuntimeError,'并非仅连接一个光驱'):
                optical.preflight()

    def test_invalid_state_rejected_before_firmware_access(self):
        with patch.object(optical,'supported',side_effect=AssertionError('must not inspect firmware')):
            with self.assertRaises(ValueError):optical.switch('force')
