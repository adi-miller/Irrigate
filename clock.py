import time
from datetime import datetime

import pytz


class SystemClock:
    def __init__(self, timezone="UTC"):
        self.timezone = pytz.timezone(timezone)

    def monotonic(self):
        return time.monotonic()

    def now(self):
        return datetime.now(self.timezone)

    def sleep(self, seconds):
        time.sleep(seconds)
