from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from panasonic_on_ubuntu.charge_habits import Habits,predict,soon
from panasonic_on_ubuntu.care import Policy


class HabitTests(unittest.TestCase):
    def series(self):
        day=datetime(2026,11,9,7)
        events=[(day-timedelta(days=i*7)+timedelta(hours=2,minutes=m)).timestamp() for i,m in ((1,0),(2,10),(3,-10))]
        return day.timestamp(),events
    def test_consistent_weekday_forecast_and_insufficient_history(self):
        now,events=self.series()
        prediction=predict(events,now)
        self.assertEqual(prediction['departure_at'],now+7200)
        self.assertEqual(prediction['observations'],3)
        self.assertTrue(soon(prediction,now))
        self.assertIsNone(predict(events[:2],now))
    def test_irregular_days_and_duplicate_events_do_not_create_confidence(self):
        now,events=self.series()
        self.assertIsNone(predict([events[0],events[0],events[0]],now))
        self.assertIsNone(predict(events[:2]+[events[2]+7200],now))
        self.assertIsNone(predict(events+[events[0]+7200],now))
    def test_observe_requires_ten_minutes_of_confirmed_battery_use(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker=Habits(Path(directory)/'history.json')
            tracker.observe({'ac':True},1,1000)
            tracker.observe({'ac':False,'usb_online':False},1,1005)
            for now in range(1010,1605,5):tracker.observe({'ac':False},0,now)
            self.assertEqual(tracker.events,[])
            tracker.observe({'ac':False},0,1605)
            self.assertEqual(tracker.events,[1005])
            self.assertEqual(json.loads(tracker.path.read_text())['departures'],[1005])
    def test_short_disconnect_gap_and_forced_drain_not_learned(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker=Habits(Path(directory)/'history.json')
            for data,driver,now in (({'ac':True},1,100),({'ac':False},1,105),({'ac':True},1,110),
                                    ({'ac':False},2,115),({'ac':False},0,900)):
                tracker.observe(data,driver,now)
            self.assertFalse(tracker.events)
    def test_prediction_delays_only_scheduled_maintenance(self):
        now,events=self.series();pred=predict(events,now)
        config={'mode':'care','limit':80,'maintenance_days':60}
        state={'ac_since':now-80*3600,'last_maintenance':now-61*86400}
        battery=dict(capacity=81,present=True,ac=True,status='Not charging',watts=0,departure_prediction=pred)
        p=Policy(state)
        self.assertEqual(p.tick(config,battery,1,now),1)
        self.assertNotIn('maintenance_phase',p.state)
        battery['departure_prediction']=None
        self.assertEqual(p.tick(config,battery,1,now+5),0)
        self.assertEqual(p.phase,'maintenance-full')
        p=Policy(state)
        battery['departure_prediction']=pred
        self.assertEqual(p.tick({**config,'action':'full','request_id':'now'},battery,1,now),0)
        self.assertEqual(p.phase,'full')
    def test_future_stale_or_invalid_samples_are_ignored(self):
        now,events=self.series()
        self.assertIsNone(predict([now+1,now-60*86400,'bad',float('nan')],now))
        self.assertFalse(soon({'departure_at':now+9*3600},now))
