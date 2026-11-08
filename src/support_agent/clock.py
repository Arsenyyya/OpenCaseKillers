from datetime import datetime, timezone


class Clock:
    def now(self):
        return datetime.now(timezone.utc)


class FrozenClock(Clock):
    def __init__(self, now):
        self.value = now

    def now(self):
        return self.value
