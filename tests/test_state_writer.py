import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from panasonic_on_ubuntu.state_writer import JsonWriter


class WriterTests(unittest.TestCase):
    def test_duplicate_ownership_is_not_rewritten_but_external_change_is_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'; writer=JsonWriter(path,0o600)
            value={'last':{'limit':'20'}}
            self.assertTrue(writer.write(value))
            before=path.stat().st_ino
            self.assertFalse(writer.write(value))
            self.assertEqual(path.stat().st_ino,before)
            path.write_text('{}')
            self.assertTrue(writer.write(value))
            self.assertEqual(json.loads(path.read_text()),value)
            path.unlink()
            self.assertTrue(writer.write(value))

    def test_heartbeat_and_changes_never_publish_stale_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'; writer=JsonWriter(path,heartbeat=5)
            with patch('panasonic_on_ubuntu.state_writer.time.monotonic',side_effect=[1,3,6,7]):
                self.assertTrue(writer.write({'timestamp':100,'watts':2}))
                self.assertFalse(writer.write({'timestamp':102,'watts':2}))
                self.assertTrue(writer.write({'timestamp':105,'watts':2}))
                self.assertTrue(writer.write({'timestamp':106,'watts':3}))
            self.assertEqual(json.loads(path.read_text()),{'timestamp':106,'watts':3})
