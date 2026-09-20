from dataclasses import dataclass
from typing import Optional


class Job:
  def __init__(self, valve, duration, sched):
    self.valve = valve
    self.duration = duration
    self.sched = sched
    self.sensor = valve.sensor if hasattr(valve, "sensor") and sched is not None else None


@dataclass
class Operation:
  identifier: int
  job: Job
  kind: str
  duration_seconds: float
  deadline: float
  last_positive: Optional[float]
  open_seconds: float = 0.0
  liters: float = 0.0
  attributable_seconds: float = 0.0
  complete: bool = True
  quality_reason: Optional[str] = None
  paused: bool = False
  cancelled: bool = False
  no_flow: bool = False

  @property
  def valve(self):
    return self.job.valve
