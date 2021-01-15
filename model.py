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
  def __init__(self, name, type, start, duration, days, seasons):
    self.name = name
    self.type = type
    self.start = start
    self.duration = duration
    self.days = days
    self.seasons = seasons
    self.sensor = None

class Sensor:
  def __init__(self, type, handler):
    self.enabled = True
    self.type = type
    self.handler = handler

class Job:
  def __init__(self, valve, duration, sched, sensor = None):
    self.valve = valve
    self.duration = duration
    self.sched = sched
    self.sensor = sensor
