import time
from test_base import assertValves
from test_base import setStartTimeToNow
from test_base import init

def test_schedSimple():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1')
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=10)
  valves = cfg.valves
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 0
  time.sleep(60)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  assert valves['valve1'].openSeconds == 60

def test_schedThirdWaiting():
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
  time.sleep(60)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (True, True)])
  assert len(q.queue) == 0
  time.sleep(60)
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

  valves = cfg.valves
  assertValves(valves, ['valve1'], [(False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(3)
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
  assert valves['valve1'].openSeconds == 240

def test_schedOverlap():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1')
  setStartTimeToNow(cfg, 'sched2')
  cfg.valves['valve1'].schedules["sched2"] = cfg.schedules['sched2']
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  valves = cfg.valves
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (False, False), (False, False)])
  assert len(q.queue) == 0
  time.sleep(121)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert valves['valve1'].openSeconds == 120
  assert len(q.queue) == 0

