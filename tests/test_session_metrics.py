from types import SimpleNamespace as NS
import threading
import unittest
from unittest.mock import Mock, patch
from panasonic_on_ubuntu import session_metrics as metrics


class SessionTests(unittest.TestCase):
    def test_network_client_is_reused_on_own_context_and_released(self):
        client=Mock()
        client.wait_shutdown.side_effect=lambda integrate,cancel,done:done(None,None)
        with patch.object(metrics.NM.Client,'new_async',side_effect=lambda cancel,done:done(None,None)) as new, \
             patch.object(metrics.NM.Client,'new_finish',return_value=client), \
             patch.object(metrics.NM.Client,'wait_shutdown_finish'):
            session=metrics.DesktopConnections()
            try:
                owners=[session.read('network',lambda obj:(obj,threading.get_ident())) for _ in range(3)]
                self.assertTrue(all(obj is client for obj,_ in owners))
                self.assertEqual(len(set(owner for _,owner in owners)),1)
                self.assertNotEqual(owners[0][1],threading.get_ident())
                new.assert_called_once()
            finally:session.close()
            self.assertFalse(session.thread.is_alive())
            client.wait_shutdown.assert_called_once()

    def test_failed_initialization_can_retry_without_stale_cache(self):
        client=Mock()
        client.wait_shutdown.side_effect=lambda integrate,cancel,done:done(None,None)
        with patch.object(metrics.NM.Client,'new_async',side_effect=lambda cancel,done:done(None,None)), \
             patch.object(metrics.NM.Client,'new_finish',side_effect=[RuntimeError('offline'),client]), \
             patch.object(metrics.NM.Client,'wait_shutdown_finish'):
            session=metrics.DesktopConnections()
            try:
                with self.assertRaisesRegex(RuntimeError,'offline'):session.read('network',lambda obj:True)
                self.assertTrue(session.read('network',lambda obj:True))
            finally:session.close()

    def test_audio_reuses_connection_invalidates_on_events_and_recovers(self):
        pulse=Mock()
        pulse.server_info.return_value=NS(default_sink_name='speaker',default_source_name='mic')
        item=NS(index=1,name='speaker',description='Speaker',mute=False,
                volume=NS(values=[.2,.4]),state=NS(_value='running'))
        pulse.sink_list.return_value=[item]
        pulse.source_list.return_value=[]
        with patch('pulsectl.Pulse',return_value=pulse) as factory:
            connection=metrics.PulseConnection()
            first=connection.read()
            self.assertEqual(first['sinks'][0]['volume'],30)
            self.assertEqual(first['sinks'][0]['state'],'RUNNING')
            first['sinks'][0]['volume']=99
            self.assertEqual(connection.read()['sinks'][0]['volume'],30)
            pulse.sink_list.assert_called_once()
            item.volume.values=[.6,.6]
            pulse.event_callback_set.call_args.args[0](None)
            self.assertEqual(connection.read()['sinks'][0]['volume'],60)
            self.assertEqual(pulse.sink_list.call_count,2)
            pulse.server_info.side_effect=RuntimeError('server disconnected')
            with self.assertRaises(RuntimeError):connection.read()
            self.assertIsNone(connection.cached)
            pulse.server_info.side_effect=None
            self.assertEqual(connection.read()['sinks'][0]['volume'],60)
            self.assertEqual(factory.call_count,2)
            connection.close()

    def test_disappeared_bluetooth_owner_hides_stale_objects(self):
        manager=Mock()
        manager.get_name_owner.return_value=None
        self.assertEqual(metrics.bluez_objects(manager),{})
        manager.get_objects.assert_not_called()
