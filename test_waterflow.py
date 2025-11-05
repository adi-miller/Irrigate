from test_base import init
from test_base import setStartTimeToNow
from datetime import datetime

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

def test_waterflow_history_structure():
  """Test that waterflow history contains timestamp and value tuples"""
  irrigate, logger, cfg, valves, q = init("test_config.json")
  
  # Get the history
  history = irrigate.waterflow.getHistory()
  
  # Should have 120 entries
  assert len(history) == 120
  
  # Each entry should be a dict with timestamp and value
  for entry in history:
    assert isinstance(entry, dict)
    assert "timestamp" in entry
    assert "value" in entry
    assert isinstance(entry["value"], (int, float))
    # Timestamp should be parseable as ISO format
    timestamp = datetime.fromisoformat(entry["timestamp"])
    assert isinstance(timestamp, datetime)
  
  # Add a value and check it's added correctly
  irrigate.waterflow.setLastLiter_1m(5.5)
  import time
  time.sleep(61)  # Wait for more than 60 seconds
  irrigate.waterflow.setLastLiter_1m(6.5)
  
  history = irrigate.waterflow.getHistory()
  # Should still be 120 (deque maxlen)
  assert len(history) == 120
  # Last entry should be our new value
  assert history[-1]["value"] == 6.5

