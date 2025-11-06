import time
from test_base import init
from test_base import assertValves
from test_base import setStartTimeToNow

def test_sh_mqttQueue():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  cfg.valves['Test1'].schedules.clear()
  cfg.valves['Test2'].schedules.clear()
  cfg.valves['Test3'].schedules.clear()
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.mqtt.processMessages("xxx/queue/Test1/command", 0.2)
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (False, False), (False, False)])
  assert len(q.queue) == 0
  return irrigate, logger, valves, q, cfg

def test_sh_mqttQueueDisabled():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  cfg.valves['Test1'].schedules.clear()
  cfg.valves['Test2'].schedules.clear()
  cfg.valves['Test3'].schedules.clear()
  cfg.valves['Test1'].enabled = False
  irrigate.start()
  time.sleep(4)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.mqtt.processMessages("xxx/queue/Test1/command", 1)
  time.sleep(4)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0

def test_sh_mqttDisable():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  cfg.valves['Test1'].schedules.clear()
  cfg.valves['Test2'].schedules.clear()
  cfg.valves['Test3'].schedules.clear()
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.mqtt.processMessages("xxx/enabled/Test1/command", 0)
  time.sleep(3)
  irrigate.mqtt.processMessages("xxx/queue/Test1/command", 1)
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  irrigate.mqtt.processMessages("xxx/enabled/Test1/command", 1)
  time.sleep(3)
  irrigate.mqtt.processMessages("xxx/queue/Test1/command", 1)
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (False, False), (False, False)])
  assert len(q.queue) == 0

def test_sh_mqttDisableAfterQueue():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  cfg.valves['Test1'].enabled = False
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0

def test_sh_mqttQueue2():
  irrigate, logger, valves, q, cfg = test_sh_mqttQueue()
  irrigate.mqtt.processMessages("xxx/queue/Test2/command", 0.2)
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 0
  return irrigate, logger, valves, q, cfg

def test_mqttQueue3():
  irrigate, logger, valves, q, cfg = test_sh_mqttQueue2()
  irrigate.mqtt.processMessages("xxx/queue/Test3/command", 1)
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1
  time.sleep(10)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (True, True)])
  assert len(q.queue) == 0

def test_sh_mqttDisableWhileInQueue():
  irrigate, logger, valves, q, cfg = test_sh_mqttQueue2()
  irrigate.mqtt.processMessages("xxx/queue/Test3/command", 1)
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1
  cfg.valves['Test1'].enabled = False
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (True, True), (True, True)])
  assert len(q.queue) == 0

# Test removed: test_sh_mqttSuspend
# Suspend functionality has been removed from the system

def test_sh_sensorOverridesMqtt():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test5')
  cfg.valves['Test1'].schedules.clear()
  cfg.valves['Test2'].schedules.clear()
  cfg.valves['Test3'].schedules.clear()
  cfg.valves['Test4'].schedules.clear()
  cfg.valves['Test5'].sensor.disable = True
  irrigate.start()
  time.sleep(3)
  # Should be handled but not opened
  assertValves(valves, ['Test5'], [(True, False)])

  # Sensor is disabling - valve stays closed
  time.sleep(3)
  assertValves(valves, ['Test5'], [(True, False)])

  cfg.valves['Test5'].sensor.disable = False
  time.sleep(3)
  # Should open because the sensor is now enabled
  assertValves(valves, ['Test5'], [(True, True)])

def test_sh_mqttErrors():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test1', duration=0.5)
  setStartTimeToNow(cfg, 'Test2', deltaInMinutes=10)
  assertValves(valves, ['Test1', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.start()
  time.sleep(2)
  irrigate.mqtt.processMessages("xxx/queue/Test1/command", "asd")
  time.sleep(1)
  irrigate.mqtt.processMessages("xxx/enable/Test1/command", "")
  time.sleep(1)
  irrigate.mqtt.processMessages("xxx/enable/Test1/command", 4)
  time.sleep(1)
  irrigate.mqtt.processMessages("xxx/enable/asd/command", 4)
  time.sleep(1)
  irrigate.mqtt.processMessages("xxx/enable/valve786/command", 4)
  time.sleep(1)
  irrigate.mqtt.processMessages("", "")
  time.sleep(1)
  irrigate.mqtt.processMessages("/", 4)
  time.sleep(30)
  assert valves['Test1'].secondsDaily == 30
