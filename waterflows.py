import math
import threading
from collections import deque

from paho.mqtt import client

from clock import SystemClock


class BaseWaterflow:
  FRESHNESS_SECONDS = 60
  IDLE_HEARTBEAT_SECONDS = 600
  IDLE_HEARTBEAT_GRACE_SECONDS = 60

  def __init__(self, logger, config, clock=None):
    self.logger = logger
    self.config = config
    self.clock = clock or SystemClock()
    self._enabled = config.enabled
    self.type = config.type
    self.leakdetection = getattr(config, "leakdetection", False)
    self._started = False
    self._connected = False
    self._lastLiter_1m = 0.0
    self._lastupdate = None
    self._received = None
    self._invalid_reading = False
    self._opening_deadline = None
    self._history_received = None
    self._history = deque(maxlen=120)
    self._lock = threading.RLock()
    self._samples = deque(maxlen=2048)
    self._availability = deque([(self.clock.monotonic(), False)], maxlen=2048)
    self.last_error = None

  def _record_availability(self):
    with self._lock:
      self._availability.append((
        self.clock.monotonic(), self._enabled and self._started and self._connected,
      ))

  @property
  def enabled(self):
    return self._enabled

  @enabled.setter
  def enabled(self, value):
    with self._lock:
      self._enabled = value
      self._record_availability()

  @property
  def started(self):
    return self._started

  @started.setter
  def started(self, value):
    with self._lock:
      self._started = value
      self._record_availability()

  @property
  def connected(self):
    return self._connected

  @connected.setter
  def connected(self, value):
    with self._lock:
      self._connected = value
      self._record_availability()

  def lastLiter_1m(self):
    """Last observation, not a freshness assertion; consumers must read snapshot."""
    with self._lock:
      return self._lastLiter_1m

  def setLastLiter_1m(self, value):
    try:
      if isinstance(value, bool):
        raise ValueError("Water flow must be a finite nonnegative number")
      value = float(value)
      if not math.isfinite(value) or value < 0:
        raise ValueError("Water flow must be a finite nonnegative number")
    except (ValueError, TypeError, OverflowError):
      with self._lock:
        self._invalid_reading = True
      raise
    now = self.clock.monotonic()
    timestamp = self.clock.now().replace(tzinfo=None)
    with self._lock:
      self._lastLiter_1m = value
      self._lastupdate = timestamp
      self._received = now
      self._invalid_reading = False
      self._opening_deadline = None
      self._samples.append((now, value))
      self.last_error = None
      if self._history_received is None or now - self._history_received >= 60:
        self._history.append((timestamp, value))
        self._history_received = now

  def snapshot(self):
    with self._lock:
      age = None if self._received is None else max(0.0, self.clock.monotonic() - self._received)
      fresh = age is not None and age <= self.FRESHNESS_SECONDS
      available = self.enabled and self.started and self.connected and fresh
      reason = None
      if not self.enabled:
        reason = "disabled"
      elif not self.started or not self.connected:
        reason = "disconnected"
      elif age is None:
        reason = "no valid reading"
      elif not fresh:
        reason = "stale reading"
      return {
        "value": self._lastLiter_1m if self._received is not None else None,
        "received": self._received, "timestamp": self._lastupdate,
        "available": bool(available), "fresh": fresh,
        "age_seconds": age, "reason": reason, "enabled": self.enabled,
      }

  def get_health(self, *, idle=False, active=False, startup_since=None):
    with self._lock:
      sample = self.snapshot()
      source = {"available": sample["available"], "reason": sample["reason"]}
      if self.enabled and self.started and self.connected:
        now = self.clock.monotonic()
        if self._invalid_reading:
          source = {"available": False, "reason": "invalid reading"}
        elif (idle or active) and self._opening_deadline is not None:
          available = now <= self._opening_deadline
          source = {"available": available, "reason": None if available else "no active reading"}
        elif idle and (sample["value"] == 0 or
                       (sample["received"] is None and startup_since is not None)):
          age = sample["age_seconds"] if sample["received"] is not None else max(0.0, now - startup_since)
          available = age <= self.IDLE_HEARTBEAT_SECONDS + self.IDLE_HEARTBEAT_GRACE_SECONDS
          source = {"available": available, "reason": None if available else "missed idle heartbeat"}
      health = {key: value for key, value in sample.items() if key not in ("timestamp", "received", "value")}
      if self.enabled:
        health["source"] = source
      return health

  def expect_active(self, *, startup_since=None):
    """Spend at most one opening grace per real observation, never per poll/resume."""
    with self._lock:
      if (not self.enabled or self._opening_deadline is not None
          or (self._received is not None and self._lastLiter_1m != 0)):
        return
      if self.get_health(idle=True, startup_since=startup_since)["source"]["available"]:
        self._opening_deadline = self.clock.monotonic() + self.FRESHNESS_SECONDS

  def getHistory(self):
    with self._lock:
      return [{"timestamp": stamp.isoformat(), "value": value} for stamp, value in self._history]

  def intervals(self, start, end):
    with self._lock:
      samples = list(self._samples)
      availability = list(self._availability)
    boundaries = {start, end}
    for stamp, _ in samples:
      boundaries.update(value for value in (stamp, stamp + self.FRESHNESS_SECONDS) if start < value < end)
    boundaries.update(stamp for stamp, _ in availability if start < stamp < end)
    ordered = sorted(boundaries)
    result = []
    for left, right in zip(ordered, ordered[1:]):
      sample = next(((stamp, value) for stamp, value in reversed(samples) if stamp <= left), None)
      connected = next((state for stamp, state in reversed(availability) if stamp <= left), False)
      valid = connected and sample is not None and left < sample[0] + self.FRESHNESS_SECONDS
      result.append((left, right, sample[1] if valid else None))
    return result

  def shutdown(self, timeout=2):
    self.started = False
    self.connected = False


class TestWaterflow(BaseWaterflow):
  __test__ = False

  def __init__(self, logger, config, clock=None):
    super().__init__(logger, config, clock)
    self.exception = False

  def start(self):
    if self.exception:
      raise RuntimeError("Injected waterflow startup failure")
    self.started = True
    self.connected = True


class MqttWaterflow(BaseWaterflow):
  def __init__(self, logger, config, clock=None, client_factory=None):
    super().__init__(logger, config, clock)
    self.client_factory = client_factory
    self.mqttClient = None
    self.terminated = False

  def start(self):
    if self.started or not self.enabled:
      return
    self.terminated = False
    try:
      self.mqttClient = (self.client_factory() if self.client_factory else
                         client.Client(client.CallbackAPIVersion.VERSION1, self.config.clientname))
      self.mqttClient.on_connect = self.on_connect
      self.mqttClient.on_disconnect = self.on_disconnect
      self.mqttClient.on_message = self.on_message
      self.mqttClient.reconnect_delay_set(min_delay=1, max_delay=30)
      self.mqttClient.connect_async(self.config.hostname)
      result = self.mqttClient.loop_start()
      if result not in (None, 0):
        raise RuntimeError("MQTT flow loop failed to start: %s" % result)
      self.started = True
    except Exception:
      self.last_error = "MQTT flow startup failed"
      self.logger.exception(self.last_error)
      self.shutdown()
      raise

  def on_connect(self, mqtt_client, userdata, flags, rc):
    self.connected = rc == 0
    if self.connected:
      mqtt_client.subscribe(self.config.topic)
      self.last_error = None
    else:
      self.last_error = "MQTT connection rejected: %s" % rc
      self.logger.error(self.last_error)

  def on_disconnect(self, mqtt_client, userdata, rc):
    self.connected = False
    if not self.terminated:
      self.logger.warning("Waterflow MQTT connection lost (code %s)", rc)

  def on_message(self, mqtt_client, userdata, msg):
    try:
      self.setLastLiter_1m(msg.payload)
    except (ValueError, TypeError, OverflowError):
      self.logger.error("Invalid waterflow reading on topic %s", msg.topic)

  def shutdown(self, timeout=2):
    self.terminated = True
    super().shutdown(timeout)
    if self.mqttClient is not None:
      from mqtt import stop_client
      return stop_client(self.mqttClient, self.logger, timeout)
    return True


def waterflowFactory(type, logger, config):
  if type == "mqtt":
    return MqttWaterflow(logger, config)
  raise ValueError("Unsupported production waterflow type: %s" % type)
