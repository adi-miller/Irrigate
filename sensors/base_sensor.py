class BaseSensor():
  def __init__(self, logger, config):
    self.name = config.name
    self.logger = logger
    self.config = config
    self.enabled = config.enabled
    self.disable = False
    self.uv = 10.2
    self.exception = False
    self.started = False

class TestSensor(BaseSensor):
  # Can be called multiple times. Make sure to initialize only once
  def start(self):
    if self.exception:
      raise Exception("Test exception in sensor.start()")

    if self.started:
      return

    self.started = True
    self.logger.info("Sensor Test started.")

  # This method is called every 0.5 seconds while the valve is open, so
  # it must return quickly. If any long processing is needed, it should
  # be executed in a thread and stored to be fetched quickly by this call.
  def shouldDisable(self):
    if self.exception:
      raise Exception("Test exception in sensor.shouldDisable()")

    return self.disable

  def getUv(self):
    if self.exception:
      raise Exception("Test exception in sensor.getUv()")

    return self.uv

  def getTelemetry(self):
    testTelemetry = []
    testTelemetry["num/value"] = 42
    testTelemetry["color"] = "black"
    testTelemetry["bool/value"] = True
    return testTelemetry

def sensorFactory(type, logger, config):
  if type == 'test':
    return TestSensor(logger, config)

  if type == 'openweathermap':
    from sensors.openweathermap_sensor import OpenWeatherMapSensor
    return OpenWeatherMapSensor(logger, config)
