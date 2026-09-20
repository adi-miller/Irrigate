import math
import threading
from datetime import timedelta

import requests

from scheduling import uv_factor
from sensors.base_sensor import BaseSensor, SensorUnavailable


class OpenWeatherMapSensor(BaseSensor):
  REFRESH_SECONDS = 2 * 60 * 60
  STALE_SECONDS = 6 * 60 * 60

  def __init__(self, logger, config, clock=None, http_get=None):
    super().__init__(logger, config, clock)
    self.type = "OpenWeatherMap"
    self.apiKey = config.api_key
    self.lat = config.latitude
    self.lon = config.longitude
    precip = getattr(config, "precipitation", None)
    self.precip_days = getattr(precip, "days_to_aggregate", 3)
    self.precip_threshold = getattr(precip, "disable_threshold_mm", 1.0)
    self.http_get = http_get or requests.get
    self._lock = threading.RLock()
    self._stop = threading.Event()
    self.worker = None
    self._snapshot = None
    self.revision = 0
    self.last_error = None

  def start(self):
    if self.started or not self.enabled:
      return
    self._stop.clear()
    self.started = True
    self.worker = threading.Thread(target=self.updaterThread, name="Weather", daemon=True)
    self.worker.start()

  def shutdown(self, timeout=2):
    self._stop.set()
    self.started = False
    if self.worker and self.worker is not threading.current_thread():
      self.worker.join(timeout)
      if self.worker.is_alive():
        self.logger.error("Weather worker did not stop within the shutdown bound")
        return False
    return True

  @staticmethod
  def _nonnegative(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
      raise ValueError("%s must be numeric" % field)
    if not math.isfinite(value) or value < 0:
      raise ValueError("%s must be finite and nonnegative" % field)
    return float(value)

  def call_api(self, path, params):
    response = self.http_get(
      "https://api.openweathermap.org/data/3.0/" + path,
      params={**params, "lat": self.lat, "lon": self.lon, "appid": self.apiKey},
      timeout=(5, 10),
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
      raise ValueError("Weather response must be an object")
    return result

  def refresh(self):
    date = self.clock.now()
    days = self.precip_days
    forecast = self.call_api("onecall", {"exclude": "current,minutely,hourly", "units": "metric"})
    uv = self._nonnegative(forecast["daily"][0]["uvi"], "UV")
    precip = 0.0
    for index in range(days):
      if self._stop.is_set():
        return False
      day = (date - timedelta(days=index + 1)).strftime("%Y-%m-%d")
      result = self.call_api("onecall/day_summary", {"date": day})
      precip += self._nonnegative(result["precipitation"]["total"], "precipitation")
    if not math.isfinite(precip):
      raise ValueError("Accumulated precipitation overflow")
    with self._lock:
      if self._stop.is_set():
        return False
      self._snapshot = {
        "uv": uv, "recentPrecip": precip,
        "received": self.clock.monotonic(), "timestamp": self.clock.now().isoformat(),
        "precip_days": days,
      }
      self.revision += 1
      self.last_error = None
    return True

  def updaterThread(self):
    failures = 0
    while not self._stop.is_set():
      try:
        if not self.refresh():
          break
        failures = 0
        delay = self.REFRESH_SECONDS
      except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, OverflowError) as error:
        failures += 1
        with self._lock:
          self.last_error = "Weather refresh failed (%s)" % type(error).__name__
        self.logger.error(self.last_error)
        delay = min(60 * (2 ** min(failures - 1, 5)), self.REFRESH_SECONDS)
      except Exception as error:
        with self._lock:
          self.last_error = "Weather worker failed (%s)" % type(error).__name__
        self.logger.error(self.last_error)
        self.started = False
        return
      self._stop.wait(delay)

  def get_health(self):
    with self._lock:
      age = (None if self._snapshot is None else
             max(0.0, self.clock.monotonic() - self._snapshot["received"]))
      fresh = age is not None and age < self.STALE_SECONDS
      current_window = self._snapshot is not None and self._snapshot["precip_days"] == self.precip_days
      available = bool(self.enabled and self.started and fresh and current_window)
      reason = None
      if not self.enabled:
        reason = "disabled"
      elif not self.started:
        reason = "weather worker unavailable"
      elif age is None:
        reason = "no complete valid weather snapshot"
      elif not fresh:
        reason = "weather snapshot is stale"
      elif not current_window:
        reason = "weather snapshot uses a different precipitation window"
      return {
        "name": self.name, "enabled": self.enabled, "available": available,
        "fresh": fresh, "age_seconds": age, "reason": reason,
        "last_error": self.last_error,
        "last_update": self._snapshot["timestamp"] if self._snapshot else None,
        "stale_after_seconds": self.STALE_SECONDS,
      }

  def snapshot(self):
    with self._lock:
      health = self.get_health()
      if not health["available"]:
        raise SensorUnavailable(health["reason"])
      return dict(self._snapshot)

  def shouldDisable(self):
    return self.snapshot()["recentPrecip"] > self.precip_threshold

  def getUv(self):
    return self.snapshot()["uv"]

  def getFactor(self):
    return uv_factor(self.getUv(), self.uv_adjustments)

  def getTelemetry(self, forced=False):
    snapshot = self.snapshot()
    return {"uv": snapshot["uv"], "recentPrecip": snapshot["recentPrecip"]}
