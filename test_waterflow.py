from test_base import init
from test_base import setStartTimeToNow

def test_sh_waterflowException():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test1', deltaInMinutes=10)
  setStartTimeToNow(cfg, 'Test2', deltaInMinutes=10)
  irrigate.waterflow.exception = True
  irrigate.start()
  assert not irrigate.waterflow.started

def test_sh_waterflowLeakDetection():
  irrigate, logger, cfg, valves, q = init("test_config.json")
  setStartTimeToNow(cfg, 'Test1', deltaInMinutes=10)
  setStartTimeToNow(cfg, 'Test1', deltaInMinutes=10)
  irrigate.waterflow.exception = True
  irrigate.start()
  assert not irrigate.waterflow.started
