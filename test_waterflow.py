from test_base import init
from test_base import setStartTimeToNow

def test_sh_waterflowException():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', deltaInMinutes=10)
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=10)
  irrigate.globalWaterflow.handler.exception = True
  irrigate.start()
  assert not irrigate.globalWaterflow.handler.started 

def test_sh_waterflowLeakDetection():
  irrigate, logger, cfg, valves, q = init("test_config.yaml")
  setStartTimeToNow(cfg, 'sched1', deltaInMinutes=10)
  setStartTimeToNow(cfg, 'sched2', deltaInMinutes=10)
  irrigate.globalWaterflow.handler.exception = True
  irrigate.start()
  assert not irrigate.globalWaterflow.handler.started 
