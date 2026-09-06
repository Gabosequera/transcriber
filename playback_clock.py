"""Reloj del dispositivo reportado por ffplay; independiente del coste de spawn."""
import math
import re
import time

STATUS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s+(?:M-A|A-V):")


class AudioClock:
    def __init__(self, start=0):
        self.start=start
        self.sample=None
        self.first_clock_s=None
        self.created=time.monotonic()

    def feed(self, line, *, now=None):
        match=STATUS.match(line)
        if not match:
            return False
        value=float(match[1])
        if not math.isfinite(value):
            return False
        now=time.monotonic() if now is None else now
        self.sample=(value,now)
        if self.first_clock_s is None:
            self.first_clock_s=now-self.created
        return True

    def position(self, *, now=None):
        if self.sample is None:
            return None
        value,stamp=self.sample
        elapsed=(time.monotonic() if now is None else now)-stamp
        if elapsed > .5:
            return None  # reloj perdido: nunca avanzar por una suposición
        return self.start+max(0,value+max(0,elapsed))
