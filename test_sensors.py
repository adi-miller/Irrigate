import time
from test_base import init
from test_base import assertValves
from test_base import setStartTimeToNow

def test_sh_sensorOnOff():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test5')
  cfg.valves['Test1'].schedules.clear()
  cfg.valves['Test2'].schedules.clear()
  cfg.valves['Test3'].schedules.clear()
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['Test5'], [(True, True)])
  cfg.valves['Test5'].sensor.disable = True
  time.sleep(3)
  assertValves(valves, ['Test5'], [(True, False)])
  cfg.valves['Test5'].sensor.disable = False
  time.sleep(3)
  assertValves(valves, ['Test5'], [(True, True)])

def test_sh_sensorFactor():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test6')
  cfg.valves['Test1'].schedules.clear()
  cfg.valves['Test2'].schedules.clear()
  cfg.valves['Test3'].schedules.clear()
  cfg.valves['Test6'].sensor.uv = 0.5
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['Test6'], [(True, True)])
  time.sleep(15)
  assertValves(valves, ['Test6'], [(False, False)])
  assert valves['Test6'].secondsDaily == 12

def test_sh_sensorIgnoredOnMqttQueue():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  cfg.valves['Test1'].schedules.clear()
  cfg.valves['Test2'].schedules.clear()
  cfg.valves['Test3'].schedules.clear()
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3', 'Test5'], [(False, False), (False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.mqtt.processMessages("xxx/queue/Test5/command", 1)
  time.sleep(3)
  assertValves(valves, ['Test5'], [(True, True)])
  cfg.valves['Test5'].sensor.disable = True
  time.sleep(3)
  assertValves(valves, ['Test5'], [(True, True)])

def test_sh_mqttQueueOnSensorDisabled():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  cfg.valves['Test1'].schedules.clear()
  cfg.valves['Test2'].schedules.clear()
  cfg.valves['Test3'].schedules.clear()
  cfg.valves['Test5'].sensor.disable = True
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['Test1', 'Test2', 'Test3', 'Test5'], [(False, False), (False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.mqtt.processMessages("xxx/queue/Test5/command", 1)
  time.sleep(3)
  assertValves(valves, ['Test5'], [(True, True)])

def test_sh_scheduleSensorShouldDisable():
  # This test validates that when the sensor is ShouldDisable, then the valve doesn't even open initially
  # (this needs to be verified by viewing the logs), but does get queued so that if the sensor turns to
  # ShouldDisable == False, then the valve opens.
  irrigate, logger, cfg, valves, q = init("test_config.json")
  cfg.valves['Test1'].schedules.clear()
  cfg.valves['Test2'].schedules.clear()
  cfg.valves['Test3'].schedules.clear()
  cfg.valves['Test4'].schedules.clear()
  setStartTimeToNow(cfg, 'Test5')
  cfg.valves['Test5'].sensor.disable = True
  irrigate.start()
  time.sleep(5)
  assertValves(valves, ['Test4', 'Test2', 'Test3'], [(False, False), (False, False), (False, False)])
  cfg.valves['Test5'].sensor.disable = False
  time.sleep(5)
  assert valves['Test5'].secondsDaily <= 5

def test_sh_badSensor():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test5', duration=0.1)
  cfg.valves['Test1'].schedules.clear()
  cfg.valves['Test2'].schedules.clear()
  cfg.valves['Test3'].schedules.clear()
  sensor = cfg.valves['Test5'].sensor
  sensor.exception = False
  irrigate.start()
  sensor.exception = True
  time.sleep(2)
  assertValves(valves, ['Test5'], [(True, True)])
  time.sleep(7)
  assertValves(valves, ['Test5'], [(False, False)])
  assert valves['Test5'].secondsDaily >= 5
