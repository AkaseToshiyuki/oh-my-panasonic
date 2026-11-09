"""Record state changes immediately and bound routine health messages."""


class StateLog:
    def __init__(self, interval=300):
        self.interval = interval
        self.key = None
        self.stamp = None

    def due(self, key, now):
        if self.stamp is None or key != self.key or not 0 <= now-self.stamp < self.interval:
            self.key, self.stamp = key, now
            return True
        return False
