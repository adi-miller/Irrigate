class Valve:
  def __init__(self, name):
    self.name = name
    self.enabled = True
    self.handled = False
    self.open = False
    self.suspended = False
    self.openSeconds = 0
    self.schedules = {}

class Schedule:
  def __init__(self, name, type, start, duration, days):
    self.name = name
    self.type = type
    self.start = start
    self.duration = duration
    self.days = days
    self.sensor = None

class Sensor:
  def __init__(self, type, handler):
    self.enabled = True
    self.type = type
    self.handler = handler

class Job:
  valve = None
  duration = 0
  sched = None

  def __init__(self, valve, duration = None, sched = None):
    if duration != None and sched != None:
      raise Exception("Unsupported")
    self.valve = valve
    if sched != None:
      if sched.sensor != None:
        self.duration = sched.duration * sched.sensor.handler.getFactor()
      else:
        self.duration = sched.duration
    else:
      self.duration = duration
    self.sched = sched

def queueJob(logger, q, job):
  exists = False
  for j in q.queue:
    if j.sched == job.sched and j.valve == job.valve:
      exists = True

  if not exists:
    q.put(job)
    if job.sched != None:
      logger.info("Valve '%s' job queued per sched '%s'. Duration %s minutes." % (job.valve.name, job.sched.name, job.duration))
    else:
      logger.info("Valve '%s' adhoc job queued. Duration %s minutes." % (job.valve.name, job.duration))
