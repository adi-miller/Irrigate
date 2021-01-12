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

class UvSensor():
  def __init__(self, logger):
    self.logger = logger
    
  def start(self):
    self.logger.info("Sensor UV started.")

  def shouldDisable(self):
    return False

  def getFactor(self):
    return 1

class TestSensor():
  def __init__(self, logger):
    self.logger = logger
    self.disable = False
    self.factor = 1
    self.started = False

  # Can be called multiple times. Make sure to initialize only once
  def start(self):
    if self.started:
      return

    self.started = True
    self.logger.info("Sensor Test started.")

  # This method is called every 0.5 seconds while the valve is open, so 
  # it must return quickly. If any long processing is needed, it should
  # be executed in a thread and stored to be fetched quickly by this call. 
  def shouldDisable(self):
    return self.disable

  def getFactor(self):
    return self.factor

def sensorFactory(type, logger, config):
  if type == 'rain':
    return RainSensor(logger)

  if type == 'uv':
    return UvSensor(logger)

  if type == 'test':
    return TestSensor(logger)

  if type == 'darksky':
    return DarkskySensor(logger, config)