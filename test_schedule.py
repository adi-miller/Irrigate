import time
import datetime
import calendar
from test_base import init
from test_base import assertValves
from test_base import setStartTimeToNow

def test_schedSimple():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', duration=0.1)
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=10)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 0
  time.sleep(6)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  assert valves['valve1'].secondsDaily >= 6

def test_schedThirdWaiting():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', duration=1.2)
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=1, duration=0.1)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 0
  time.sleep(60)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1
  time.sleep(12)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (True, True)])
  assert len(q.queue) == 0
  time.sleep(10)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0

def test_schedConflict():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', duration=3)
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=1)
  cfg.valves['valve1'].schedules['sched2'] = cfg.schedules['sched2']
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  cfg.valves['valve4'].schedules.clear()
  cfg.valves['valve5'].schedules.clear()
  assertValves(valves, ['valve1'], [(False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(5)
  assertValves(valves, ['valve1'], [(True, True)])
  time.sleep(60)
  assertValves(valves, ['valve1'], [(True, True)])
  assert len(q.queue) == 0
  time.sleep(60)
  assertValves(valves, ['valve1'], [(True, True)])
  assert len(q.queue) == 0
  time.sleep(60)
  assertValves(valves, ['valve1'], [(True, True)])
  assert len(q.queue) == 0
  time.sleep(60)
  assert len(q.queue) == 0
  assert valves['valve1'].secondsDaily == 240

def test_schedOverlap():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', duration=0.1)
  setStartTimeToNow(cfg, 'sched2', duration=0.1)
  cfg.valves['valve1'].schedules["sched2"] = cfg.schedules['sched2']
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(5)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (False, False), (False, False)])
  assert len(q.queue) == 0
  time.sleep(10)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  time.sleep(65)
  assert valves['valve1'].secondsDaily == 12
  assert len(q.queue) == 0

def test_schedDupCheck():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', duration=3)
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=0, duration=3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1
  time.sleep(60)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1
  time.sleep(60)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1

def test_schedPerDay():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', duration=1)
  cfg.valves['valve2'].schedules.clear()
  setStartTimeToNow(cfg, 'sched2', duration=1)
  cfg.schedules['sched1'].days.clear()
  dayStr = calendar.day_abbr[datetime.datetime.today().weekday()]
  cfg.schedules['sched1'].days.append(dayStr)
  cfg.schedules['sched2'].days.clear()
  dayStr = calendar.day_abbr[(datetime.datetime.today() + datetime.timedelta(days=1)).weekday()]
  cfg.schedules['sched2'].days.append(dayStr)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (False, False), (False, False)])

def test_schedPerSeason():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', duration=1)
  cfg.valves['valve2'].schedules.clear()
  setStartTimeToNow(cfg, 'sched2', duration=1)
  cfg.schedules['sched1'].seasons.clear()
  cfg.schedules['sched1'].seasons.append(irrigate.getSeason(cfg.latitude))
  cfg.schedules['sched2'].seasons.clear()
  cfg.schedules['sched2'].seasons.append(irrigate.getSeason(-1 * cfg.latitude))
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (False, False), (False, False)])
