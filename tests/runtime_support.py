import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from irrigate import Irrigate


class FakeClock:
  def __init__(self, now=None, monotonic=1000.0):
    self._now = now or datetime(2025, 6, 15, 10, tzinfo=timezone.utc)
    self._mono = monotonic
    self._lock = threading.RLock()

  def now(self):
    with self._lock:
      return self._now

  def monotonic(self):
    with self._lock:
      return self._mono

  def advance(self, seconds, wall_seconds=None):
    if seconds < 0:
      raise ValueError("Fake monotonic clock cannot move backwards")
    with self._lock:
      self._mono += seconds
      elapsed = seconds if wall_seconds is None else wall_seconds
      self._now = (self._now.astimezone(timezone.utc) + timedelta(seconds=elapsed)).astimezone(self._now.tzinfo)

  def sleep(self, seconds):
    self.advance(seconds)


def make_config(count=1, concurrency=1, sensor=True):
  result = {
    "timezone": "UTC", "max_concurrent_valves": concurrency,
    "location": {"latitude": 0.0, "longitude": 0.0},
    "mqtt": {"enabled": False, "hostname": "broker.invalid", "client_name": "fixturePi"},
    "telemetry": {"enabled": True, "idle_interval": 10, "active_interval": 1},
    "alerts": {
      "enabled": {"leak": True, "malfunction_no_flow": True, "irregular_flow": True,
                  "sensor_error": True, "system_exit": True},
      "leak_repeat_minutes": 15, "irregular_flow_threshold": 2.0,
      "leak_detection_exclusions": [], "channels": [],
    },
    "sensors": [{
      "name": "Weather", "type": "openweathermap", "enabled": True,
      "api_key": "offline-fixture", "latitude": 0.0, "longitude": 0.0,
      "precipitation": {"days_to_aggregate": 0, "disable_threshold_mm": 1.0},
      "uv_adjustments": [{"max_uv_index": 2, "multiplier": 0.2},
                         {"max_uv_index": 10, "multiplier": 2.0}],
    }] if sensor else [],
    "waterflow": {
      "type": "mqtt", "enabled": True, "leakdetection": True,
      "hostname": "broker.invalid", "clientname": "fixtureFlow", "topic": "fixture/mcu/liter_1m",
    },
    "valves": [],
  }
  for index in range(count):
    valve = {
      "name": "Valve " + chr(65 + index), "type": "3wire", "enabled": True,
      "watering_mode": "duration", "gpio_on_pin": index * 2 + 2, "gpio_off_pin": index * 2 + 3,
      "schedules": [{
        "time_based_on": "fixed", "fixed_start_time": "23:00", "duration": 5,
        "days": [], "seasons": [], "enable_uv_adjustments": False,
      }],
    }
    if sensor:
      valve["sensor"] = "Weather"
    result["valves"].append(valve)
  return result


def make_app(tmp_path, clock=None, configuration=None, **kwargs):
  path = tmp_path / "config.json"
  path.write_text(json.dumps(configuration or make_config()), encoding="utf-8")
  app = Irrigate(
    str(path), offline=True, clock=clock or FakeClock(),
    logger=logging.getLogger("offline-runtime-test"),
    data_directory=tmp_path / "data", **kwargs,
  )
  app.start(background=False)
  return app
