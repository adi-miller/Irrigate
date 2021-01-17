import time
from test_base import init
from test_base import assertValves
from test_base import setStartTimeToNow

def test_sh_mqttOpen():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.mqtt.processMessages("xxx/open/valve1/command", 0.2)
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (False, False), (False, False)])
  assert len(q.queue) == 0
  return irrigate, logger, valves, q, cfg

def test_sh_mqttOpenDisabled():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  cfg.valves['valve1'].enabled = False
  irrigate.start()
  time.sleep(4)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.mqtt.processMessages("xxx/open/valve1/command", 1)
  time.sleep(4)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0

def test_sh_mqttDisable():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.mqtt.processMessages("xxx/enabled/valve1/command", 0)
  time.sleep(3)
  irrigate.mqtt.processMessages("xxx/open/valve1/command", 1)
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  irrigate.mqtt.processMessages("xxx/enabled/valve1/command", 1)
  time.sleep(3)
  irrigate.mqtt.processMessages("xxx/open/valve1/command", 1)
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (False, False), (False, False)])
  assert len(q.queue) == 0

def test_sh_mqttDisableAfterOpen():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  cfg.valves['valve1'].enabled = False
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  assert len(q.queue) == 0

def test_sh_mqttOpen2():
  irrigate, logger, valves, q, cfg = test_sh_mqttOpen()
  irrigate.mqtt.processMessages("xxx/open/valve2/command", 0.2)
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 0
  return irrigate, logger, valves, q, cfg

def test_mqttOpen3():
  irrigate, logger, valves, q, cfg = test_sh_mqttOpen2()
  irrigate.mqtt.processMessages("xxx/open/valve3/command", 1)
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(True, True), (True, True), (False, False)])
  assert len(q.queue) == 1
  time.sleep(10)
  assertValves(valves, ['valve1', 'valve2', 'valve3'], [(False, False), (False, False), (True, True)])
  assert len(q.queue) == 0

def test_sh_mqttDisableWhileInQueue():
  irrigate, logger, valves, q, cfg = test_sh_mqttOpen2()
  irrigate.mqtt.processMessages("xxx/open/valve3/command", 1)
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
  irrigate.mqtt.processMessages("xxx/suspend/valve1/command", 1)
  time.sleep(1)
  duration = valves['valve1'].secondsDaily
  time.sleep(3)
  assert duration == valves['valve1'].secondsDaily
  irrigate.mqtt.processMessages("xxx/suspend/valve1/command", 0)
  time.sleep(3)
  assert duration < valves['valve1'].secondsDaily

def test_sh_sensorOverridesMqtt():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched4')
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  cfg.schedules['sched4'].sensor.handler.disable = True
  irrigate.start()
  time.sleep(3)
  # Should be handled but not opened
  assertValves(valves, ['valve5'], [(True, False)])

  irrigate.mqtt.processMessages("xxx/suspend/valve5/command", 0)
  time.sleep(3)
  # Should not open because the sensor overrides
  assertValves(valves, ['valve5'], [(True, False)])

  cfg.schedules['sched4'].sensor.handler.disable = False
  time.sleep(3)
  # Should open because the sensor is now enabled and the MQTT command is already "lost"
  assertValves(valves, ['valve5'], [(True, True)])
