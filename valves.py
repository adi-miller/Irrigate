import time

class BaseValve():
  def __init__(self, logger):
    self.logger = logger

  def open(self):
    self.logger.info("Opening valve")

  def close(self):
    self.logger.info("Closing valve")

class TestValve(BaseValve):
  def open(self):
    BaseValve.open(self)
    time.sleep(1)

  def close(self):
    BaseValve.close(self)
    time.sleep(1)

class ThreeWireValve(BaseValve):
  def __init__(self, logger):
    BaseValve.__init__(self, logger)

  def open(self):
    BaseValve.open(self)

  def close(self):
    BaseValve.close(self)

def valveFactory(type, logger, config):
  if type == 'test':
    return TestValve(logger)

  if type == '3wire':
    return ThreeWireValve(logger)

  raise Exception("Cannot find implementation for valve type '%s'." % type)
