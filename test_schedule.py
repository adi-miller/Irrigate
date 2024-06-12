import time
import datetime
import calendar
from test_base import init
from test_base import assertValves
from test_base import setStartTimeToNow

def test_schedSimple():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test1', duration=0.1)
  setStartTimeToNow(cfg, 'Test2', duration=0.1)
  setStartTimeToNow(cfg, 'Test3', deltaInMinutes=10)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 0
  time.sleep(6)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  assert valves['Test1'].secondsDaily >= 6

def test_schedThirdWaiting():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test1', duration=1.2)
  setStartTimeToNow(cfg, 'Test2', duration=1.2)
  setStartTimeToNow(cfg, 'Test3', deltaInMinutes=1, duration=0.1)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 0
  time.sleep(60)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1
  time.sleep(12)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (True, True)])
  assert len(q.queue) == 0
  time.sleep(10)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0

def test_schedConflict():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test1', duration=3)
  setStartTimeToNow(cfg, 'Test2', deltaInMinutes=1)
  cfg.valves['Test1'].schedules.append(cfg.valves['Test2'].schedules[0]) 
  cfg.valves['Test2'].schedules.clear()
  cfg.valves['Test3'].schedules.clear()
  cfg.valves['Test4'].schedules.clear()
  cfg.valves['Test5'].schedules.clear()
  assertValves(valves, ['Test1'], [(False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(5)
  assertValves(valves, ['Test1'], [(True, True)])
  time.sleep(60)
  assertValves(valves, ['Test1'], [(True, True)])
  assert len(q.queue) == 0
  time.sleep(60)
  assertValves(valves, ['Test1'], [(True, True)])
  assert len(q.queue) == 0
  time.sleep(60)
  assertValves(valves, ['Test1'], [(True, True)])
  assert len(q.queue) == 0
  time.sleep(60)
  assert len(q.queue) == 0
  assert valves['Test1'].secondsDaily == 240

def test_schedOverlap():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test1', duration=0.1)
  setStartTimeToNow(cfg, 'Test2', duration=0.1)
  cfg.valves['Test1'].schedules.append(cfg.valves['Test2'].schedules[0]) 
  cfg.valves['Test2'].schedules.clear()
  cfg.valves['Test3'].schedules.clear()
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(5)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (False, False), (False, False)])
  assert len(q.queue) == 0
  time.sleep(10)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  time.sleep(65)
  assert valves['Test1'].secondsDaily == 12
  assert len(q.queue) == 0

def test_schedDupCheck():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test1', duration=3)
  setStartTimeToNow(cfg, 'Test2', duration=3)
  setStartTimeToNow(cfg, 'Test3', deltaInMinutes=0, duration=3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1
  time.sleep(60)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1
  time.sleep(60)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1

def test_schedPerDay():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test1', duration=1)
  setStartTimeToNow(cfg, 'Test2', duration=1)
  cfg.valves['Test1'].schedules[0].days.clear()
  dayStr = calendar.day_abbr[datetime.datetime.today().weekday()]
  cfg.valves['Test1'].schedules[0].days.append(dayStr)
  cfg.valves['Test2'].schedules[0].days.clear()
  dayStr = calendar.day_abbr[(datetime.datetime.today() + datetime.timedelta(days=1)).weekday()]
  cfg.valves['Test2'].schedules[0].days.append(dayStr)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (False, False), (False, False)])

def test_schedPerSeason():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test1', duration=1)
  setStartTimeToNow(cfg, 'Test2', duration=1)
  cfg.valves['Test1'].schedules[0].seasons.clear()
  cfg.valves['Test1'].schedules[0].seasons.append(irrigate.getSeason(cfg.latitude))
  cfg.valves['Test2'].schedules[0].seasons.clear()
  cfg.valves['Test2'].schedules[0].seasons.append(irrigate.getSeason(-1 * cfg.latitude))
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (False, False), (False, False)])
