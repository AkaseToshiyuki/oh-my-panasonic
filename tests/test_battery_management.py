from pathlib import Path
import builtins
import json
import os
import runpy
import tempfile
import time
import unittest
from unittest.mock import patch
from panasonic_on_ubuntu.care import Policy


class ManagementTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='posix', 'Root helper uses POSIX locking')
    def test_control_round_trip_preserves_saved_preferences(self):
        from panasonic_on_ubuntu import care
        helper=runpy.run_path(str(Path(__file__).resolve().parents[1]/'packaging/pou-control'))
        main=helper['main']
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'battery-care.json';runtime=root/'runtime.json'
            firmware=root/'firmware';firmware.mkdir();(firmware/'interface_version').write_text('1')
            saved={'mode':'care','limit':85,'maintenance_days':30,'sleep_hold':True,'poweroff_hold':False}
            config.write_text(json.dumps(saved))
            def load(path,default):
                value=json.loads(config.read_text())
                return value if path==config else {'request_id':value['request_id'],'timestamp':time.time(),'phase':'system' if value['mode']=='system' else 'holding','error':''}
            def open_file(path,*args,**kwargs):
                return builtins.open(root/'control.lock' if str(path)=='/run/lock/pou-battery-control.lock' else path,*args,**kwargs)
            with patch.dict(main.__globals__,BASE=root,open=open_file), \
                    patch.multiple(care,CONFIG=config,RUNTIME=runtime,FIRMWARE=firmware,load=load), \
                    patch('os.geteuid',return_value=0),patch('socket.socket'):
                for mode in ('system','care'):
                    with patch('sys.argv',['pou-control','battery',mode]):main()
                    actual=json.loads(config.read_text())
                    self.assertEqual(actual['mode'],mode)
                    for key in ('limit','maintenance_days','sleep_hold','poweroff_hold'):
                        self.assertEqual(actual[key],saved[key])

    def test_narrow_helper_allows_resume_and_rejects_unrelated_actions(self):
        arguments=runpy.run_path(str(Path(__file__).resolve().parents[1]/'packaging/pou-adjust'))['arguments']
        for mode in ('system','care','full'):
            self.assertEqual(arguments(['battery',mode]),['battery',mode])
        for value in (['battery','maintain'], ['battery','care','85'], ['battery','unknown']):
            with self.assertRaises(ValueError):arguments(value)

    def test_resume_preserves_limit_and_does_not_restore_full_override(self):
        policy=Policy()
        sample={'present':True,'capacity':85,'ac':True,'status':'Not charging','watts':0}
        saved={'mode':'care','limit':85,'maintenance_days':60,'sleep_hold':True,'poweroff_hold':True}
        self.assertEqual(policy.tick(dict(saved,action='full',request_id='full'),sample,1,100),0)
        self.assertEqual(policy.tick(dict(saved,mode='system',action='configure',request_id='system'),sample,0,105),0)
        self.assertNotIn('override_until',policy.state)
        self.assertEqual(policy.tick(dict(saved,action='configure',request_id='resume'),sample,0,110),1)
        self.assertEqual(policy.phase,'holding')
        self.assertEqual(policy.state['limit'],85)
        self.assertNotIn('maintenance_phase',policy.state)
