import pytz
import time
import datetime
from irrigate import Irrigate 
from suntime import Sun

def assertValves(valves, valveNames, status, assumption = None):
  ord = 0
  for valveName in valveNames:
    assert valves[valveName].handled == status[ord][0], assumption
    assert valves[valveName].open == status[ord][1], assumption
    ord = ord + 1

def setStartTimeToNow(cfg, sched, deltaInMinutes = None, duration = None):
  nowTime = datetime.datetime.now()
  if deltaInMinutes != None:
    nowTime = nowTime + datetime.timedelta(minutes=deltaInMinutes)
  if duration != None:
    cfg.schedules[sched].duration = duration
  cfg.schedules[sched].start  = str(nowTime.hour) + ":" + str(nowTime.minute)

def init(configFilename):
  irrigate = Irrigate(configFilename)
  return irrigate, irrigate.logger, irrigate.cfg, irrigate.valves, irrigate.q

def test_sh_initAllNoRuns():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', deltaInMinutes=10)
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=10)
  lat, lon = cfg.getLatLon()
  assertValves(cfg.valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(2)
  assertValves(cfg.valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0

def test_suspendInTheMiddle():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1')
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  valves = cfg.valves
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (False, False), (False, False)])
  assert len(q.queue) == 0
  valves['valve1'].suspended = True
  time.sleep(7)
  valves['valve1'].suspended = False
  time.sleep(3)
  assert valves['valve1'].secondsDaily < 8
  assert len(q.queue) == 0

def test_suspendedFromStart():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1')
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  valves = cfg.valves
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  valves['valve1'].suspended = True
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(5)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, False), (False, False), (False, False)])
  valves['valve1'].suspended = False
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (False, False), (False, False)])
  assert valves['valve1'].secondsDaily < 4

def test_sunset():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  sun = Sun(cfg.latitude, cfg.longitude)
  nowTime = datetime.datetime.now() + datetime.timedelta(seconds=10)
  timezone = cfg.timezone
  nowTime = nowTime.replace(tzinfo=pytz.timezone(timezone))
  sunsetTime = sun.get_local_sunset_time().replace(tzinfo=pytz.timezone(timezone))
  if (nowTime < sunsetTime):
    offset = sunsetTime - nowTime
  else:
    offset = nowTime - sunsetTime

  if offset.seconds < 0:
    cfg.schedules['sched3'].start = "-" 
  else:
    cfg.schedules['sched3'].start = "+" 

  hours = offset.seconds // 60 // 60
  minutes = (offset.seconds - (hours * 60 * 60)) // 60
  cfg.schedules['sched3'].start = cfg.schedules['sched3'].start = "+"  + str(hours) + ":" + str(minutes)
  cfg.schedules['sched3'].duration = 1
  cfg.valves['valve4'].enabled = True
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()

  irrigate.start()
  assertValves(valves, ['valve4'], [(False, False)])
  assert len(q.queue) == 0
  time.sleep(62)
  assertValves(valves, ['valve4'], [(True, True)])
  time.sleep(60)
  assertValves(valves, ['valve4'], [(False, False)])

def test_sh_valveDisableInitially():
  # When a valve is disabled, it doesn't get scheduled so enabling it after the schedule was
  # already evaluated, does not queue it. 
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1')
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=1)
  cfg.valves['valve1'].enabled = False
  cfg.valves['valve2'].enabled = False
  cfg.valves['valve3'].enabled = False
  irrigate.start()
  time.sleep(3)
  assert len(q.queue) == 0
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  cfg.valves['valve1'].enabled = True
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])

def test_valveDisableDuring():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', duration=2)
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=1)
  valves = cfg.valves
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 0
  time.sleep(60)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1
  cfg.valves['valve1'].enabled = False
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (True, True), (True, True)])
  assert len(q.queue) == 0

def xtest_terminate():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  cfg.valvesConcurrency = 1
  setStartTimeToNow(cfg, 'sched1')
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=1)
  irrigate.start(False)
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2'], [(True, True), (True, True)])
  assert len(q.queue) == 0
  irrigate.terminated = True
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2'], [(False, False), (False, False)])

def test_everyXMinutes():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  test1 = 0
  test2 = 0
  for i in range(13):
    if irrigate.everyXMinutes("test1", 0.05, False):
      test1 += 1
    if irrigate.everyXMinutes("test2", 0.2, True):
      test2 += 1
    time.sleep(1)
  assert test1 == 4
  assert test2 == 2
