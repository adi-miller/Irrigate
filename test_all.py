from threading import Thread
from suntime import Sun
import pytz
import time
import config
import queue
import logging
from logic import Irrigate 
import datetime
import mqtt
import model

def assertValves(valves, valveNames, status):
  ord = 0
  for valveName in valveNames:
    assert valves[valveName].handled == status[ord][0]
    assert valves[valveName].open == status[ord][1]
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
  return irrigate, irrigate.logger, irrigate.cfg, irrigate.sun, irrigate.valves, irrigate.q

def test_sh_initAllNoRuns():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', deltaInMinutes=10)
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=10)
  lat, lon = cfg.getLatLon()
  sun = Sun(lat, lon)
  assertValves(cfg.valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(5)
  assertValves(cfg.valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0

def test_schedSimple():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1')
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=10)
  sun = Sun(cfg.latitude, cfg.longitude)
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
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', duration=2)
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=1)
  sun = Sun(cfg.latitude, cfg.longitude)
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
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', duration=3)
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=1)
  cfg.valves['valve1'].schedules['sched2'] = cfg.schedules['sched2']
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  cfg.valves['valve4'].schedules.clear()
  cfg.valves['valve5'].schedules.clear()
  sun = Sun(cfg.latitude, cfg.longitude)

  # sched = logic.initThreads(logger, cfg, sun, q, False)
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
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1')
  setStartTimeToNow(cfg, 'sched2')
  cfg.valves['valve1'].schedules["sched2"] = cfg.schedules['sched2']
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  sun = Sun(cfg.latitude, cfg.longitude)
  # sched = logic.initThreads(logger, cfg, sun, q, False)
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

def test_suspendInTheMiddle():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1')
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  sun = Sun(cfg.latitude, cfg.longitude)
  # sched = logic.initThreads(logger, cfg, sun, q, False)
  valves = cfg.valves
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(15)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (False, False), (False, False)])
  assert len(q.queue) == 0
  valves['valve1'].suspended = True
  time.sleep(15)
  valves['valve1'].suspended = False
  time.sleep(31)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert valves['valve1'].openSeconds == 45
  assert len(q.queue) == 0

def test_suspendedFromStart():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1')
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  sun = Sun(cfg.latitude, cfg.longitude)
  # sched = logic.initThreads(logger, cfg, sun, q, False)
  valves = cfg.valves
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  valves['valve1'].suspended = True
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(15)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  valves['valve1'].suspended = False
  time.sleep(15)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (False, False), (False, False)])
  time.sleep(31)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert valves['valve1'].openSeconds == 45
  assert len(q.queue) == 0

def test_sunset():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
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

  # sched = logic.initThreads(logger, cfg, sun, q, True)
  irrigate.start()
  assertValves(valves, ['valve4'], [(False, False)])
  assert len(q.queue) == 0
  time.sleep(62)
  assertValves(valves, ['valve4'], [(True, True)])
  time.sleep(60)
  assertValves(valves, ['valve4'], [(False, False)])

def test_sh_mqttOpen():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  sun = Sun(cfg.latitude, cfg.longitude)
  # sched = logic.initThreads(logger, cfg, sun, q, True)
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  mqtt.processMessages(logger, valves, q, irrigate.queueJob, "xxx/open/valve1/command", 1)
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (False, False), (False, False)])
  assert len(q.queue) == 0
  return irrigate, logger, valves, q, cfg

def test_sh_mqttOpenDisabled():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  cfg.valves['valve1'].enabled = False
  sun = Sun(cfg.latitude, cfg.longitude)
  # sched = logic.initThreads(logger, cfg, sun, q, True)
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  mqtt.processMessages(logger, valves, q, irrigate.queueJob, "xxx/open/valve1/command", 1)
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0

def test_sh_mqttDisable():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  sun = Sun(cfg.latitude, cfg.longitude)
  # sched = logic.initThreads(logger, cfg, sun, q, True)
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  mqtt.processMessages(logger, valves, q, irrigate.queueJob, "xxx/enabled/valve1/command", 0)
  time.sleep(3)
  mqtt.processMessages(logger, valves, q, irrigate.queueJob, "xxx/open/valve1/command", 1)
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  mqtt.processMessages(logger, valves, q, irrigate.queueJob, "xxx/enabled/valve1/command", 1)
  time.sleep(3)
  mqtt.processMessages(logger, valves, q, irrigate.queueJob, "xxx/open/valve1/command", 1)
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (False, False), (False, False)])
  assert len(q.queue) == 0

def test_sh_mqttDisableAfterOpen():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  cfg.valves['valve1'].enabled = False
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0

def test_sh_mqttOpen2():
  irrigate, logger, valves, q, cfg = test_sh_mqttOpen()
  mqtt.processMessages(logger, valves, q, irrigate.queueJob, "xxx/open/valve2/command", 1)
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 0
  return irrigate, logger, valves, q, cfg

def test_mqttOpen3():
  irrigate, logger, valves, q, cfg = test_sh_mqttOpen2()
  mqtt.processMessages(logger, valves, q, irrigate.queueJob, "xxx/open/valve3/command", 1)
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1
  time.sleep(60)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (True, True)])
  assert len(q.queue) == 0

def test_sh_mqttDisableWhileInQueue():
  irrigate, logger, valves, q, cfg = test_sh_mqttOpen2()
  mqtt.processMessages(logger, valves, q, irrigate.queueJob, "xxx/open/valve3/command", 1)
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1
  cfg.valves['valve1'].enabled = False
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (True, True), (True, True)])
  assert len(q.queue) == 0

def test_sh_mqttSuspend():
  irrigate, logger, valves, q, cfg = test_sh_mqttOpen()
  time.sleep(2)
  mqtt.processMessages(logger, valves, q, irrigate.queueJob, "xxx/suspend/valve1/command", 1)
  time.sleep(1)
  duration = valves['valve1'].openSeconds
  time.sleep(3)
  assert duration == valves['valve1'].openSeconds
  mqtt.processMessages(logger, valves, q, irrigate.queueJob, "xxx/suspend/valve1/command", 0)
  time.sleep(3)
  assert duration < valves['valve1'].openSeconds

def test_sh_sensorOnOff():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched4')
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  sun = Sun(cfg.latitude, cfg.longitude)
  # sched = logic.initThreads(logger, cfg, sun, q, True)
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve5'], [(True, True)])
  cfg.schedules['sched4'].sensor.handler.disable = True
  time.sleep(3)
  assertValves(valves, ['valve5'], [(True, False)])
  cfg.schedules['sched4'].sensor.handler.disable = False
  time.sleep(3)
  assertValves(valves, ['valve5'], [(True, True)])

def test_sh_sensorFactor():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched4')
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  sun = Sun(cfg.latitude, cfg.longitude)
  cfg.schedules['sched4'].sensor.handler.factor = 0.1
  # sched = logic.initThreads(logger, cfg, sun, q, True)
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve5'], [(True, True)])
  time.sleep(5)
  assertValves(valves, ['valve5'], [(False, False)])
  assert valves['valve5'].openSeconds == 6

def test_sh_sensorIgnoredOnMqtt():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  sun = Sun(cfg.latitude, cfg.longitude)
  # sched = logic.initThreads(logger, cfg, sun, q, True)
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3', 'valve5'], [(False, False), (False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  mqtt.processMessages(logger, valves, q, irrigate.queueJob, "xxx/open/valve5/command", 1)
  time.sleep(3)
  assertValves(valves, ['valve5'], [(True, True)])
  cfg.schedules['sched4'].sensor.handler.disable = True
  time.sleep(3)
  assertValves(valves, ['valve5'], [(True, True)])

def test_valveDisableInitially():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  cfg.valvesConcurrency = 2

  setStartTimeToNow(cfg, 'sched1')
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=1)
  cfg.valves['valve1'].enabled = False
  cfg.valves['valve2'].enabled = False
  cfg.valves['valve3'].enabled = False
  sun = Sun(cfg.latitude, cfg.longitude)
  # sched = logic.initThreads(logger, cfg, sun, q, True)
  irrigate.start()
  time.sleep(3)
  assert len(q.queue) == 0
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  time.sleep(60)
  assert len(q.queue) == 0
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])

def test_valveDisableDuring():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', duration=2)
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=1)
  sun = Sun(cfg.latitude, cfg.longitude)
  # sched = logic.initThreads(logger, cfg, sun, q, False)
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
  time.sleep(55)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (True, True)])
  assert len(q.queue) == 0
  time.sleep(5)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0

def xtest_mix():
  irrigate, logger, cfg, sun, valves, q = init("test_config.yaml")
  cfg.valvesConcurrency = 1
  setStartTimeToNow(cfg, 'sched1')
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=1)
  cfg.schedules['sched1'].sensor.handler.factor = 1.5
  cfg.schedules['sched2'].sensor.handler.factor = 0.5
  cfg.valves['valve3'].enabled = False
  cfg.valves['valve4'].enabled = False
  sun = Sun(cfg.latitude, cfg.longitude)
  # sched = logic.initThreads(logger, cfg, sun, q, True)
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3', 'valve5'], [(False, False), (False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
