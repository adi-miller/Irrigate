import time
import RPi.GPIO as GPIO

class BaseValve():
  def __init__(self, logger, config):
    self.logger = logger
    self.config = config

  def open(self):
    self.logger.info("Opening valve")

  def close(self):
    self.logger.info("Closing valve")

class TestValve(BaseValve):
  def open(self):
    BaseValve.open(self)
    time.sleep(0.5)

  def close(self):
    BaseValve.close(self)
    time.sleep(0.5)

class ThreeWireValve(BaseValve):
  def __init__(self, logger, config):
    BaseValve.__init__(self, logger, config)
    self.gpioOn = config['gpioOn']
    self.gpioOff = config['gpioOff']
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(self.gpioOn, GPIO.OUT)
    GPIO.setup(self.gpioOff, GPIO.OUT)

  def open(self):
    BaseValve.open(self)
    GPIO.output(self.gpioOn, GPIO.HIGH)
    time.sleep(0.5)
    GPIO.output(self.gpioOn, GPIO.LOW)

  def close(self):
    BaseValve.close(self)
    GPIO.output(self.gpioOff, GPIO.HIGH)
    time.sleep(0.5)
    GPIO.output(self.gpioOff, GPIO.LOW)

def valveFactory(type, logger, config):
  if type == 'test':
    return TestValve(logger, config)

  if type == '3wire':
    return ThreeWireValve(logger, config)

  raise Exception("Cannot find implementation for valve type '%s'." % type)
