import tempfile
import unittest
from pathlib import Path
from panasonic_on_ubuntu.camera import colour_camera


class CameraTests(unittest.TestCase):
    def test_prefers_colour_sibling_and_excludes_metadata_and_ir(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);video=root/'class';video.mkdir()
            for i,name,index,bus in ((0,'External Webcam',0,'external'),(1,'IR Camera',0,'internal'),(2,'USB FHD Camera',1,'internal'),(3,'USB FHD Camera',0,'internal')):
                usb=root/bus;usb.mkdir(exist_ok=True);(usb/'idVendor').write_text('1234')
                entry=usb/f'video{i}';entry.mkdir();(entry/'name').write_text(name);(entry/'index').write_text(str(index))
                (video/entry.name).symlink_to(entry,target_is_directory=True)
            self.assertEqual(colour_camera(video),Path('/dev/video3'))

    def test_missing_camera_fails_without_fixed_usb_address(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):colour_camera(Path(directory))
