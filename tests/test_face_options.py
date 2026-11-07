import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock, MagicMock

from panasonic_on_ubuntu import face_privilege as auth, face_poses as poses, power


class PrivilegeTests(unittest.TestCase):
    def test_rebrand_retains_ownership_of_existing_face_templates(self):
        for label in ('Panasonic On Ubuntu','Oh My Panasonic'):
            for suffix in ('',' #'+'a'*32):
                self.assertTrue(auth.face.app_model({'user':'fixture','label':label+suffix},'fixture'))
                self.assertFalse(auth.face.app_model({'user':'other','label':label+suffix},'fixture'))
        self.assertFalse(auth.face.app_model({'user':'fixture','label':'Other application'},'fixture'))

    def test_user_options_are_independent_and_default_off(self):
        with tempfile.TemporaryDirectory() as d, patch.object(auth,'PREFS',Path(d)):
            self.assertEqual(auth.preferences(1000),[])
            auth.set_option(1000,'sudo',True)
            auth.set_option(1000,'polkit-1',True)
            auth.set_option(1000,'sudo',False)
            self.assertEqual(auth.preferences(1000),['polkit-1'])
            self.assertEqual(auth.preferences(1001),[])
            auth.set_option(1000,'polkit-1',False)
            self.assertFalse(list(Path(d).iterdir()))

    def test_custom_mfa_stack_is_preserved(self):
        base = 'auth [success=1 default=ignore] pam_unix.so nullok\nauth requisite pam_deny.so\nauth required pam_permit.so\n'
        with patch('pathlib.Path.read_text',return_value=base):
            self.assertTrue(auth.standard_password_stack())
        with patch('pathlib.Path.read_text',return_value=base+'auth required pam_u2f.so\n'):
            self.assertFalse(auth.standard_password_stack())

    def trial(self, reply, match, sessions, peer=True):
        import os
        from types import SimpleNamespace
        uid=os.getuid()
        fake=SimpleNamespace(pw_uid=max(1000,uid),pw_name='fixture')
        left,right=socket.socketpair()
        # Use real stream I/O; emulate SO_PEERCRED for a namespaced test UID.
        conn=MagicMock(wraps=left)
        conn.getsockopt.return_value=auth.struct.pack('3i',111 if peer else 112,fake.pw_uid,0)
        from contextlib import nullcontext
        listener=Mock()
        listener.accept.side_effect=[(conn,None),socket.timeout()]
        def respond():
            with right:
                if not right.recv(8192).endswith(b'\n'): return
                right.sendall(reply)
        if peer:
            thread=threading.Thread(target=respond);thread.start()
        with (
            left,
            patch.object(auth,'standard_password_stack',return_value=True),
            patch.object(auth.pwd,'getpwnam',return_value=fake),
            patch.object(auth,'preferences',return_value=['sudo']),
            patch.object(auth.face,'enabled',return_value=True),
            patch.object(auth.face,'active_session',side_effect=sessions),
            patch.object(auth,'sudo_command',return_value='sudo id'),
            patch.object(auth,'shell_peer',return_value=111),
            patch.object(auth,'consent_channel',return_value=nullcontext(listener)),
            patch.object(auth.face,'call',return_value=SimpleNamespace(returncode=0 if match else 1)) as camera,
        ):
            try:
                result=auth.confirmed_match(dict(service='sudo',user='fixture',tty='pts/1'),Path('/proc/1'),lambda **msg:None)
            except socket.timeout:
                result=False
        if peer: thread.join(2)
        else: right.close()
        return result,camera.call_count

    def test_decline_never_opens_camera(self):
        self.assertEqual(self.trial(b'password\n',True,['7']),(False,0))

    def test_untrusted_compositor_never_opens_camera(self):
        self.assertEqual(self.trial(b'face\n',True,['7'],False),(False,0))

    def test_match_requires_same_session_after_consent_and_capture(self):
        self.assertEqual(self.trial(b'face\n',True,['7','7','7']),(True,1))
        self.assertEqual(self.trial(b'face\n',False,['7','7']),(False,1))
        self.assertEqual(self.trial(b'face\n',True,['7','8']),(False,0))
        self.assertEqual(self.trial(b'face\n',True,['7','7','8']),(False,1))


class PoseTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript("""CREATE TABLE face_models(id INTEGER PRIMARY KEY,user,label,embedder_model,device_id);
          CREATE TABLE face_embeddings(id INTEGER PRIMARY KEY,model_id,sealed,embedding);
          INSERT INTO face_models VALUES(1,'u','Panasonic On Ubuntu','model',NULL);""")
        self.db.executemany('INSERT INTO face_embeddings VALUES(?,1,1,?)',[(i,b'\x02'+bytes([i])*2076) for i in range(10)])
        self.db.commit()
        self.original=poses.templates(self.db,'u')
        self.candidate=(self.original[0],self.original[1][::-1])

    def test_keeps_six_anchors_four_reversible_poses_and_total(self):
        with patch.object(poses.subprocess,'run',return_value=Mock(returncode=0)), self.db:
            poses.replace_poses(self.db,'u',self.original,self.candidate)
        after=poses.templates(self.db,'u')[1]
        self.assertEqual(len(after),10)
        self.assertEqual(after[:6],self.original[1][:6])
        self.assertNotEqual(after[6:],self.original[1][6:])
        self.assertEqual(self.db.execute('SELECT * FROM pou_original_poses ORDER BY id').fetchall(),self.original[1][6:])

    def test_reject_different_person_or_changed_source(self):
        with patch.object(poses.subprocess,'run',return_value=Mock(returncode=1)), self.assertRaises(RuntimeError), self.db:
            poses.replace_poses(self.db,'u',self.original,self.candidate)
        self.assertEqual(poses.templates(self.db,'u'),self.original)
        with self.db: self.db.execute('DELETE FROM face_embeddings WHERE id=0')
        with patch.object(poses.subprocess,'run',return_value=Mock(returncode=0)), self.assertRaises(RuntimeError), self.db:
            poses.replace_poses(self.db,'u',self.original,self.candidate)


class PowerTests(unittest.TestCase):
    def test_missing_platform_counter_preserves_package_reading(self):
        cols='PkgWatt Bzy_MHz Avg_MHz Busy% PkgTmp'.split()
        row='2.3 1200 400 3 41'.split()
        for v in ('nan','-','0'):
            self.assertIsNone(power.counter_sample(cols+['SysWatt'],row+[v])['platform_w'])
        self.assertEqual(power.counter_sample(cols,row)['watts'],2.3)
        self.assertEqual(power.counter_sample(cols+['SysWatt'],row+['7.8'])['platform_w'],7.8)
