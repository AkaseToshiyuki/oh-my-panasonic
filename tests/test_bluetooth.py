import unittest
from unittest.mock import Mock
import gi
gi.require_version('Gtk','4.0');gi.require_version('Adw','1')
from gi.repository import GLib
from panasonic_on_ubuntu.bluetooth_page import Devices,allowed,reply_value


class PairingTests(unittest.TestCase):
    def test_both_owner_and_user_selected_device_required(self):
        self.assertTrue(allowed(':1.22',':1.22','/dev/one','/dev/one'))
        for sender,owner,device,selected in [(':1.23',':1.22','/dev/one','/dev/one'),(':1.22',':1.22','/dev/two','/dev/one'),(':1.22',':1.22','/dev/one',None),('','','/dev/one','/dev/one')]:
            self.assertFalse(allowed(sender,owner,device,selected))
    def test_pin_and_passkey_validation_preserves_leading_zero(self):
        self.assertEqual(reply_value('RequestPinCode','0012').unpack(),('0012',))
        self.assertEqual(reply_value('RequestPasskey','001234').unpack(),(1234,))
        for method,text in [('RequestPinCode',''),('RequestPinCode','a'*17),('RequestPinCode','\x00'),('RequestPasskey','1000000'),('RequestPasskey','-123'),('RequestPasskey','１２３')]:
            with self.assertRaises(ValueError):reply_value(method,text)
    def test_agent_rejects_other_apps_devices_and_closed_windows(self):
        controller=Devices.__new__(Devices);controller.owner=':1.22';controller.closed=False;controller.selected='/dev/one'
        for sender,path in [(':1.23','/dev/one'),(':1.22','/dev/two')]:
            invocation=Mock();controller.agent(None,sender,'','', 'RequestConfirmation',GLib.Variant('(ou)',(path,123456)),invocation)
            invocation.return_dbus_error.assert_called_once();invocation.return_value.assert_not_called()
        controller.closed=True;invocation=Mock()
        controller.agent(None,':1.22','','','Cancel',GLib.Variant('()',()),invocation)
        invocation.return_dbus_error.assert_called_once()
    def test_cancel_answers_pending_invocation_once(self):
        controller=Devices.__new__(Devices);controller.prompt_timer=0;controller.dialog=Mock();controller.pending=Mock()
        invocation=controller.pending;dialog=controller.dialog
        controller.reject_prompt();controller.reject_prompt()
        invocation.return_dbus_error.assert_called_once();dialog.close.assert_called_once()
