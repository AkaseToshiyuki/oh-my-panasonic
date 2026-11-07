import contextlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from panasonic_on_ubuntu import face


class GreeterTests(unittest.TestCase):
    def check(self,changes=None,environment=None,worker=('123','456')):
        data=dict(Name='gdm-greeter',User='60578',Remote='no',Type='wayland',Class='greeter',Seat='seat0',Active='yes',Service='gdm-launch-environment',TTY='tty1',VTNr='1')
        data.update(changes or {})
        env=dict(XDG_SEAT='seat0',PAM_TTY='/dev/tty1');env.update(environment or {})
        with patch.dict(os.environ,env,clear=True),patch.object(face,'gdm_worker_identity',return_value=worker),patch.object(face,'call',side_effect=[SimpleNamespace(stdout='c1\n'),SimpleNamespace(stdout='\n'.join(f'{k}={v}' for k,v in data.items()))]):
            return face.greeter_session()
    def test_dynamic_greeter_uid_supported(self):
        self.assertEqual(self.check(),('c1',('123','456')))
        self.assertIsNotNone(self.check({'Name':'gdm','User':'975'}))
    def test_remote_user_wrong_vt_and_wrong_service_rejected(self):
        for data in ({'Remote':'yes'},{'Name':'user'},{'User':'0'},{'Class':'user'},{'Seat':'seat1'},{'Type':'tty'},{'Active':'no'},{'Service':'sshd'},{'TTY':'tty2'},{'VTNr':'2'}):
            with self.subTest(data=data):self.assertIsNone(self.check(data))
    def test_only_real_gdm_worker_on_local_seat_can_enter(self):
        self.assertIsNone(self.check(worker=None))
        for env in ({'XDG_SEAT':'seat1'},{'PAM_TTY':'pts/1'},{'PAM_TTY':':0'},{'PAM_TTY':''}):
            self.assertIsNone(self.check(environment=env))
    def test_locked_user_path_remains_fast_and_separate(self):
        with patch.object(face,'active_session',return_value='s2'),patch.object(face,'greeter_session') as greeter:
            self.assertEqual(face.authentication_context(1000),('lock','s2'));greeter.assert_not_called()
    def test_greeter_path_only_after_locked_user_gate(self):
        with patch.object(face,'active_session',return_value=None),patch.object(face,'greeter_session',return_value=('c1',('123','456'))):
            self.assertEqual(face.authentication_context(1000),('login',('c1',('123','456'))))


class AuthenticationTests(unittest.TestCase):
    def attempt(self,contexts,match=True,enabled=True,environment=None):
        output=io.StringIO();env=dict(PAM_SERVICE='gdm-password',PAM_TYPE='auth',PAM_USER='fixture',PAM_RHOST='');env.update(environment or {})
        with patch.dict(os.environ,env,clear=True),patch.object(face.os,'geteuid',return_value=0),patch.object(face.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=1000,pw_name='fixture')),patch.object(face,'enabled',return_value=enabled),patch.object(face,'authentication_context',side_effect=contexts),patch.object(face,'authenticate',return_value=SimpleNamespace(returncode=0 if match else 1)) as camera,patch('syslog.syslog'),contextlib.redirect_stdout(output):
            result=face.check_locked()
        return result,output.getvalue(),camera.call_count
    def test_first_login_and_lock_require_matching_fresh_context(self):
        for context in (('lock','s2'),('login',('c1',('123','456')))):
            ok,text,calls=self.attempt([context,context])
            self.assertTrue(ok);self.assertEqual(calls,1)
            self.assertEqual(text.count('正在尝试人脸解锁'),1)
    def test_session_switch_or_worker_replacement_rejects_match(self):
        for after in (None,('lock','s3'),('login',('c1',('123','457')))):
            self.assertFalse(self.attempt([('login',('c1',('123','456'))),after])[0])
    def test_disabled_remote_and_other_pam_never_start_camera(self):
        self.assertEqual(self.attempt([],enabled=False)[2],0)
        for env in ({'PAM_RHOST':'remote'},{'PAM_SERVICE':'sudo'},{'PAM_TYPE':'account'}):
            self.assertEqual(self.attempt([],environment=env)[2],0)
        self.assertEqual(self.attempt([None])[2],0)
    def test_failed_match_keeps_password_fallback(self):
        ok,text,calls=self.attempt([('lock','s2')],match=False)
        self.assertFalse(ok);self.assertEqual(calls,1);self.assertIn('请使用密码继续',text)
    def test_extension_has_no_second_lock_prompt(self):
        root=Path(__file__).resolve().parents[1]
        self.assertNotIn('LockHint',(root/'extension/extension.js').read_text())
        self.assertNotIn('正在尝试人脸解锁',(root/'extension/face.js').read_text())
