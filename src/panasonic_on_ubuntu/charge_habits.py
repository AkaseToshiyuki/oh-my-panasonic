"""Local, conservative departure prediction for postponing periodic maintenance."""
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
from statistics import median
import time

HISTORY = Path('/var/lib/panasonic-on-ubuntu/charging-habits.json')


def predict(events, now):
    today = datetime.fromtimestamp(now)
    days = {}
    for stamp in events:
        if type(stamp) not in (int,float) or not math.isfinite(stamp) or not now-56*86400 <= stamp < now:
            continue
        date = datetime.fromtimestamp(stamp)
        days.setdefault(date.date(),[]).append(date.hour*60+date.minute)
    for offset in (0,1):
        date = (today+timedelta(days=offset)).date()
        samples = [median(times) for day,times in days.items()
                   if day < today.date() and day.weekday() == date.weekday() and max(times)-min(times) <= 30]
        if len(samples) < 3 or max(samples)-min(samples) > 90:
            continue
        minute = round(median(samples))
        departure = datetime.combine(date,datetime.min.time())+timedelta(minutes=minute)
        stamp = departure.timestamp()
        if now-2*3600 <= stamp <= now+24*3600:
            return dict(departure_at=stamp, observations=len(samples),
                        label=('今天 ' if offset == 0 else '明天 ')+departure.strftime('%H:%M'))
    return None


def soon(prediction, now):
    if not prediction: return False
    delta = prediction['departure_at']-now
    return -2*3600 <= delta <= 8*3600


class Habits:
    def __init__(self, path=None):
        self.path = path or HISTORY
        self.zone = list(time.tzname)
        try: record = json.loads(self.path.read_text())
        except (OSError, ValueError): record = {}
        self.events = record.get('departures',[]) if isinstance(record,dict) and record.get('timezone') == self.zone else []
        if not isinstance(self.events,list): self.events=[]
        self.connected = None
        self.last_seen = None
        self.pending = None

    def observe(self, data, driver, now, enabled=True):
        valid = [stamp for stamp in self.events if type(stamp) in (int,float) and math.isfinite(stamp)
                 and now-56*86400 <= stamp <= now][-180:]
        changed = valid != self.events
        self.events = valid
        connected = True if data.get('ac') or data.get('usb_online') is True else (None if driver == 2 else False)
        if not enabled or connected is None or self.last_seen is None or not 0 <= now-self.last_seen <= 30:
            self.connected = connected if enabled else None
            self.pending = None
        elif connected:
            self.pending = None
        elif self.connected is True:
            self.pending = now
        elif self.pending is not None and now-self.pending >= 600:
            self.events.append(self.pending)
            self.pending = None
            changed = True
        self.connected = connected if enabled else None
        self.last_seen = now
        if changed:
            from .care import atomic
            atomic(self.path,{'version':1,'timezone':self.zone,'departures':self.events},0o600)
        return predict(self.events,now) if enabled else None
