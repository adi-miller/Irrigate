class RainSensor():
  def shouldDisable(self):
    return False

  def getFactor(self):
    return 1

class UvSensor():
  def shouldDisable(self):
    return False

  def getFactor(self):
    return 1

class TestSensor():
  def __init__(self):
    self.disable = False
    self.factor = 1

  def shouldDisable(self):
    return self.disable

  def getFactor(self):
    return self.factor

def sensorFactory(type):
  if type == 'rain':
    return RainSensor()

  if type == 'uv':
    return UvSensor()

  if type == 'test':
    return TestSensor()