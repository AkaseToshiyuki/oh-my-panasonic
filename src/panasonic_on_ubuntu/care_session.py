"""Carry observed AC-session duration through short, same-boot service restarts."""
import json
import math
from pathlib import Path
import time


def clock():
    return (Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            time.clock_gettime(time.CLOCK_BOOTTIME))


def continuous(record, state, wall, boot_id, boot_seconds):
    try:
        then, elapsed, since = record['wall'], record['boot_seconds'], record['ac_since']
        if any(type(v) not in (int, float) or not math.isfinite(v)
               for v in (then, elapsed, since, wall, boot_seconds)):
            return False
        return (record['boot_id'] == boot_id and state.get('ac_since') == since
                and 0 <= boot_seconds-elapsed <= 30 and 0 <= wall-then <= 30
                and abs((wall-then)-(boot_seconds-elapsed)) <= 3 and since <= then)
    except (KeyError, TypeError):
        return False


class Session:
    def __init__(self, path):
        self.path = path
        try:
            self.record = json.loads(path.read_text())
        except (OSError, ValueError):
            self.record = {}

    def prepare(self, state, now):
        self.boot_id, self.boot_seconds = clock()
        if not continuous(self.record, state, now, self.boot_id, self.boot_seconds):
            state.pop('ac_since', None)

    def checkpoint(self, state, now, valid):
        self.record = dict(wall=now, boot_id=self.boot_id, boot_seconds=self.boot_seconds,
                           ac_since=state.get('ac_since')) if valid and 'ac_since' in state else {}
        # This heartbeat lives on /run, not on the SSD; it is invalid after boot.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.new')
        tmp.write_text(json.dumps(self.record))
        tmp.chmod(0o600)
        tmp.replace(self.path)
