class Job:
  def __init__(self, valve, duration, sched):
    self.valve = valve
    self.duration = duration
    self.sched = sched
    self.sensor = valve.sensor if hasattr(valve, "sensor") and sched is not None else None
