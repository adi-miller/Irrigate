import time
from test_base import init
from test_base import assertValves
from test_base import setStartTimeToNow

def test_sh_sensorOnOff():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched4')
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
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
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched4')
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  cfg.schedules['sched4'].sensor.handler.factor = 0.2
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve5'], [(True, True)])
  time.sleep(15)
  assertValves(valves, ['valve5'], [(False, False)])
  assert valves['valve5'].openSeconds == 12

def test_sh_sensorIgnoredOnMqttOpen():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3', 'valve5'], [(False, False), (False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.mqtt.processMessages("xxx/open/valve5/command", 1)
  time.sleep(3)
  assertValves(valves, ['valve5'], [(True, True)])
  cfg.schedules['sched4'].sensor.handler.disable = True
  time.sleep(3)
  assertValves(valves, ['valve5'], [(True, True)])

def test_sh_mqttOpenOnSensorDisabled():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  cfg.schedules['sched4'].sensor.handler.disable = True
  irrigate.start()
  time.sleep(3)
  assertValves(valves, ['valve1', 'valve2', 'valve3', 'valve5'], [(False, False), (False, False), (False, False), (False, False)])
  assert len(q.queue) == 0
  irrigate.mqtt.processMessages("xxx/open/valve5/command", 1)
  time.sleep(3)
  assertValves(valves, ['valve5'], [(True, True)])

def test_sh_scheduleSensorShouldDisable():
  # This test validates that when the sensor is ShouldDisable, then the valve doesn't even open initially 
  # (this needs to be verified by viewing the logs), but does get queued so that if the sensor turns to
  # ShouldDisable == False, then the valve opens. 
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  cfg.valves['valve1'].schedules.clear()
  cfg.valves['valve2'].schedules.clear()
  cfg.valves['valve3'].schedules.clear()
  cfg.valves['valve4'].schedules.clear()
  setStartTimeToNow(cfg, 'sched4')
  cfg.schedules['sched4'].sensor.handler.disable = True
  irrigate.start()
  time.sleep(5)
  assertValves(valves, ['valve4', 'valve2', 'valve3'], [(False, False), (False, False), (False, False)])
  cfg.schedules['sched4'].sensor.handler.disable = False
  time.sleep(5)
  assert valves['valve5'].openSeconds <= 5
