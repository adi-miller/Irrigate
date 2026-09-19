from clock import SystemClock
from scheduling import uv_factor


class SensorUnavailable(RuntimeError):
  pass


class BaseSensor:
  def __init__(self, logger, config, clock=None):
    self.name = config.name
    self.logger = logger
    self.config = config
    self.clock = clock or SystemClock()
    self.enabled = config.enabled
    self.started = False
    self.uv_adjustments = getattr(config, "uv_adjustments", [])

  def shutdown(self, timeout=2):
    self.started = False


class TestSensor(BaseSensor):
  __test__ = False

  def __init__(self, logger, config, clock=None):
    super().__init__(logger, config, clock)
    self.type = "OpenWeatherMap"
    self.exception = False
    self.disable = False
    self.uv = 2.5
    self.recentPrecip = 0.0
    self.precip_days = getattr(getattr(config, "precipitation", None), "days_to_aggregate", 3)
    self.precip_threshold = getattr(getattr(config, "precipitation", None), "disable_threshold_mm", 1.0)
    self.revision = 1

  def start(self):
    if self.exception:
      raise SensorUnavailable("Injected sensor startup failure")
    self.started = True

  def _check(self):
    if self.exception or not self.started:
      raise SensorUnavailable("Simulated weather unavailable")

  def shouldDisable(self):
    self._check()
    return self.disable or self.recentPrecip > self.precip_threshold

  def getUv(self):
    self._check()
    return self.uv

  def getFactor(self):
    return uv_factor(self.getUv(), self.uv_adjustments)

  def getTelemetry(self, forced=False):
    self._check()
    return {"uv": self.uv, "recentPrecip": self.recentPrecip}

  def get_health(self):
    available = self.enabled and self.started and not self.exception
    return {"name": self.name, "enabled": self.enabled, "available": available,
            "fresh": available, "age_seconds": 0.0 if available else None,
            "reason": None if available else "simulated weather unavailable"}


def sensorFactory(type, logger, config):
  if type == "openweathermap":
    from sensors.openweathermap_sensor import OpenWeatherMapSensor
    return OpenWeatherMapSensor(logger, config)
  raise ValueError("Unsupported production sensor type: %s" % type)
