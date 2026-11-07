import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from panasonic_on_ubuntu.wifi import NM,GLib,SECURITY,security,profile,personal_profile,password_valid,Activation


class AP:
    def __init__(self,flags=SECURITY.KEY_MGMT_PSK,raw=b'pou-test'):
        self.flags=flags;self.raw=raw
    def get_ssid(self):return GLib.Bytes.new(self.raw)
    def get_rsn_flags(self):return self.flags
    def get_wpa_flags(self):return 0
    def get_flags(self):return 1 if self.flags else 0
    def get_path(self):return '/ap'


class ProfileTests(unittest.TestCase):
    def test_hidden_profile_sets_probe_ssid_without_downgrading_auth(self):
        for kind in ('wpa-psk','sae'):
            p=personal_profile('私人网络'.encode(),kind,'safe-password',hidden=True)
            self.assertTrue(p.verify());self.assertTrue(p.get_setting_wireless().get_hidden())
            self.assertEqual(p.get_setting_wireless_security().get_key_mgmt(),kind)
        for raw in ('过长'*20,'','bad\x00name'):
            with self.assertRaises(ValueError):personal_profile(raw.encode(),'open',hidden=True)
    def test_profile_keeps_raw_ssid_and_limits_saved_permissions(self):
        raw=b'\xffhidden-display';p=profile(AP(raw=raw),'safe-test-password',False,'testuser')
        self.assertTrue(p.verify())
        self.assertEqual(p.get_setting_wireless().get_ssid().get_data(),raw)
        c=p.get_setting_connection()
        self.assertFalse(c.get_autoconnect());self.assertEqual(c.get_num_permissions(),1)
        public=p.to_dbus(NM.ConnectionSerializationFlags.NO_SECRETS).unpack()
        self.assertEqual(public['connection']['permissions'],['user:testuser:'])
        self.assertNotIn('psk',public['802-11-wireless-security'])
        self.assertEqual(p.get_setting_wireless_security().get_psk(),'safe-test-password')
    def test_open_and_owe_profiles_do_not_accept_passwords(self):
        for flags in (0,SECURITY.KEY_MGMT_OWE):
            self.assertTrue(profile(AP(flags),'').verify())
            with self.assertRaises(ValueError):profile(AP(flags),'unexpected')
    def test_sae_requires_protected_management_and_keeps_strongest_mode(self):
        ap=AP(SECURITY.KEY_MGMT_PSK|SECURITY.KEY_MGMT_SAE)
        self.assertEqual(security(ap),'sae')
        p=profile(ap,'strong-password')
        self.assertEqual(p.get_setting_wireless_security().props.pmf,NM.SettingWirelessSecurityPmf.REQUIRED)
        self.assertEqual(p.get_setting_wireless_security().props.key_mgmt,'sae')
    def test_enterprise_and_invalid_credentials_never_form_personal_profile(self):
        for value in ('short','x'*65,'\x00'*8):
            self.assertFalse(password_valid('wpa-psk',value))
        self.assertTrue(password_valid('wpa-psk','a'*64))
        self.assertFalse(password_valid('wpa-psk','z'*64))
        with self.assertRaises(ValueError):profile(AP(SECURITY.KEY_MGMT_802_1X),'abcdefgh')
        for raw in (b'',b'a'*33):
            with self.assertRaises(ValueError):profile(AP(raw=raw),'abcdefgh')


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.client=Mock();self.changed=Mock();self.finished=Mock()
        self.conn=Mock();self.device=Mock();self.ap=AP()
        self.client.checkpoint_create_finish.return_value.get_path.return_value='/checkpoint'
        self.active=Mock();self.active.get_state.return_value=NM.ActiveConnectionState.ACTIVATING
        self.client.add_and_activate_connection2_finish.return_value=(self.active,None)
        self.client.activate_connection_finish.return_value=self.active
        self.client.checkpoint_rollback_finish.return_value={'/device':0}
        for name,value in (('timeout_add_seconds',42),('source_remove',True)):
            p=patch('panasonic_on_ubuntu.wifi.GLib.'+name,return_value=value);p.start();self.addCleanup(p.stop)
        self.task=Activation(self.client,self.device,self.ap,self.conn,True,True,self.changed,self.finished)
    def prepared(self):self.task.start();self.task.prepared(self.client,None)
    def started(self):self.prepared();self.task.started(self.client,None)
    def rollback(self):
        callback=self.client.checkpoint_rollback.call_args.args[-1]
        callback(self.client,None)
    def test_request_ack_is_not_success_and_profile_is_volatile(self):
        self.started();self.finished.assert_not_called()
        self.assertEqual(self.client.add_and_activate_connection2.call_args.args[3].unpack(),{'persist':'volatile'})
        self.conn.clear_secrets.assert_called_once()
        self.client.checkpoint_destroy.assert_not_called()
    def test_hidden_activation_has_no_fake_access_point(self):
        self.task.ap=None;self.prepared()
        self.assertIsNone(self.client.add_and_activate_connection2.call_args.args[2])
    def test_success_commits_checkpoint_before_saving_profile(self):
        self.started();self.task.state_changed(self.active,NM.ActiveConnectionState.ACTIVATED,0)
        self.client.checkpoint_destroy.assert_called_once();self.finished.assert_not_called()
        self.active.get_connection.return_value.update2.assert_not_called()
        self.task.committed(self.client,None)
        remote=self.active.get_connection.return_value
        remote.update2.assert_called_once()
        self.task.saved(remote,None)
        self.finished.assert_called_once_with(True,'已连接')
    def test_failed_activation_restores_original_and_never_saves(self):
        self.started();self.task.state_changed(self.active,NM.ActiveConnectionState.DEACTIVATED,0)
        self.rollback();self.assertFalse(self.finished.call_args.args[0])
        self.client.checkpoint_destroy.assert_not_called()
        self.active.get_connection.assert_not_called()
    def test_cancel_during_checkpoint_creation_never_activates(self):
        self.task.start();self.task.cancel();self.task.prepared(self.client,None)
        self.client.add_and_activate_connection2.assert_not_called()
        self.rollback();self.finished.assert_called_once()
    def test_cancel_before_activation_ack_never_commits_late_success(self):
        self.prepared();self.task.cancel()
        self.client.checkpoint_rollback.assert_not_called()
        self.active.get_state.return_value=NM.ActiveConnectionState.ACTIVATED
        self.task.started(self.client,None)
        self.rollback()
        self.client.checkpoint_destroy.assert_not_called();self.finished.assert_called_once()
    def test_timeout_before_ack_waits_for_server_then_rolls_back(self):
        self.prepared();self.task.expired()
        self.client.checkpoint_rollback.assert_not_called()
        self.task.started(self.client,None);self.rollback()
        self.assertFalse(self.finished.call_args.args[0]);self.assertIn('连接超时',self.finished.call_args.args[1])
    def test_timeout_and_failed_restore_are_reported_honestly(self):
        self.started();self.task.expired()
        self.client.checkpoint_rollback_finish.return_value={'/device':1}
        self.rollback()
        self.assertIn('恢复尚未确认',self.finished.call_args.args[1])
        self.assertFalse(self.finished.call_args.args[0])
    def test_checkpoint_failure_never_disconnects_current_network(self):
        self.client.checkpoint_create_finish.side_effect=GLib.Error('denied')
        self.prepared();self.client.add_and_activate_connection2.assert_not_called()
        self.client.checkpoint_rollback.assert_not_called();self.finished.assert_called_once()
    def test_existing_connection_never_gets_deleted_or_rewritten(self):
        self.task.new=False;self.started()
        self.client.activate_connection_async.assert_called_once()
        self.client.add_and_activate_connection2.assert_not_called()
        self.task.state_changed(self.active,NM.ActiveConnectionState.ACTIVATED,0)
        self.task.committed(self.client,None)
        self.active.get_connection.assert_not_called()
        self.finished.assert_called_once_with(True,'已连接')
