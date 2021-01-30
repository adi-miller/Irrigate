from sensors.darksky_sensor import DarkskySensor

class RainSensor():
  def __init__(self, logger):
    self.logger = logger

  def start(self):
    self.logger.info("Sensor Rain started.")

  def shouldDisable(self):
    return False

  def getFactor(self):
    return 1

  def getTelemetry(self):
    pass

class UvSensor():
  def __init__(self, logger):
    self.logger = logger
    
  def start(self):
    self.logger.info("Sensor UV started.")

  def shouldDisable(self):
    return False

  def getFactor(self):
    return 1

  def getTelemetry(self):
    pass

class TestSensor():
  def __init__(self, logger):
    self.logger = logger
    self.disable = False
    self.factor = 1
    self.exception = False
    self.started = False

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

  def getFactor(self):
    if self.exception:
      raise Exception("Test exception in sensor.getFactor()")

    return self.factor

  def getTelemetry(self):
    testTelemetry = []
    testTelemetry["num/value"] = 42
    testTelemetry["color"] = "black"
    testTelemetry["bool/value"] = True
    return testTelemetry

def sensorFactory(type, logger, config):
  if type == 'rain':
    return RainSensor(logger)

  if type == 'uv':
    return UvSensor(logger)

  if type == 'test':
    return TestSensor(logger)

  if type == 'darksky':
    return DarkskySensor(logger, config)
