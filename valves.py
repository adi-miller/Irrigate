import importlib
import math
import threading

from clock import SystemClock


_pulse_lock = threading.RLock()


class BaseValve:
  offline_safe = False
  def __init__(self, logger, config, clock=None):
    self.logger = logger
    self.config = config
    self.clock = clock or SystemClock()
    self.name = config.name
    self.enabled = config.enabled
    self.handled = False
    self.is_open = False
    self.secondsDaily = 0
    self.litersDaily = 0.0
    self.secondsRemain = 0
    self.secondsDuration = 0
    self.secondsLast = 0
    self.litersLast = 0.0
    self.schedules = config.schedules
    self.waterflow = None
    self.baseline_lpm = None
    self.baseline_trend = None
    self.baseline_std_dev = None
    self.baseline_sample_count = 0
    self.initialized = False

  def initialize(self):
    raise NotImplementedError("A valve driver must implement initialization")

  def open(self):
    raise NotImplementedError("A valve driver must implement opening")

  def close(self):
    raise NotImplementedError("A valve driver must implement closing")


class TestValve(BaseValve):
  __test__ = False
  offline_safe = True

  def __init__(self, logger, config, clock=None):
    super().__init__(logger, config, clock)
    self.calls = []

  def initialize(self):
    self.initialized = True

  def open(self):
    self.calls.append(("open", self.clock.monotonic()))

  def close(self):
    self.calls.append(("close", self.clock.monotonic()))


class ThreeWireValve(BaseValve):
  def __init__(self, logger, config, gpio=None, clock=None):
    super().__init__(logger, config, clock)
    self.gpioOn = config.gpio_on_pin
    self.gpioOff = config.gpio_off_pin
    duration = float(getattr(config, "pulse_duration", 0.02))
    if not math.isfinite(duration) or duration <= 0:
      raise ValueError("pulse_duration must be finite and positive")
    self.pulseDuration = min(duration, 0.2)
    self.gpio = gpio
    self.offline_safe = gpio is not None and not getattr(gpio, "__name__", "").startswith("RPi")

  def initialize(self):
    with _pulse_lock:
      if self.initialized:
        return
      if self.gpio is None:
        try:
          self.gpio = importlib.import_module("RPi.GPIO")
        except (ImportError, RuntimeError) as error:
          raise RuntimeError("Production requires the real RPi.GPIO adapter; use --test for offline mode") from error
      if self.gpio.HIGH == self.gpio.LOW:
        raise RuntimeError("Invalid GPIO adapter: HIGH and LOW must be distinct")
      self.gpio.setmode(self.gpio.BCM)
      self.gpio.setwarnings(False)
      self.gpio.setup(self.gpioOn, self.gpio.OUT, initial=self.gpio.LOW)
      self.gpio.setup(self.gpioOff, self.gpio.OUT, initial=self.gpio.LOW)
      self.initialized = True

  def _pulse(self, pin):
    with _pulse_lock:
      self.initialize()
      self._deenergize()
      try:
        self.gpio.output(pin, self.gpio.HIGH)
        self.clock.sleep(self.pulseDuration)
      finally:
        self._deenergize()

  def _deenergize(self):
    errors = []
    for pin in (self.gpioOn, self.gpioOff):
      try:
        self.gpio.output(pin, self.gpio.LOW)
      except Exception as error:
        self.logger.error("Could not de-energize GPIO %s (%s)", pin, type(error).__name__)
        errors.append(error)
    if errors:
      raise RuntimeError("GPIO de-energization failed; do not energize the opposite coil") from errors[0]

  def open(self):
    self._pulse(self.gpioOn)

  def close(self):
    self._pulse(self.gpioOff)


def valveFactory(type, logger, config):
  if type == "3wire":
    return ThreeWireValve(logger, config)
  raise ValueError("Unsupported production valve type: %s" % type)
