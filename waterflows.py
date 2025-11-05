import time
import threading
from datetime import datetime
from datetime import timedelta
from paho.mqtt import client
from collections import deque
import random

class BaseWaterflow():
  def __init__(self, logger, config):
    self.logger = logger
    self.config = config
    self.enabled = config.enabled
    self.type = config.type
    self.leakdetection = config.leakdetection
    self.started = False
    self._lastLiter_1m = 0
    self._lastupdate = datetime.now()
    self._lastHistoryUpdate = datetime.now()
    self._history = deque(maxlen=120)  # Store last 120 minutes of flow data as (timestamp, value) tuples
    # Initialize with 120 zero values with timestamps going back 120 minutes
    now = datetime.now()
    for i in range(120):
      # Start from 119 minutes ago up to current minute
      timestamp = now - timedelta(minutes=119-i)
      self._history.append((timestamp, 0.0))

  def lastLiter_1m(self):
    now = datetime.now()
    
    # If no update for more than 60 seconds, flow is 0
    if now > self._lastupdate + timedelta(0, 60):
      # Update history with 0 if enough time has passed
      if now > self._lastHistoryUpdate + timedelta(seconds=60):
        self._history.append((now, 0.0))
        self._lastHistoryUpdate = now      
      return 0

    return self._lastLiter_1m

  def setLastLiter_1m(self, value):
    self._lastLiter_1m = value
    self._lastupdate = datetime.now()
    
    # Only add to history once per minute
    now = datetime.now()
    if now > self._lastHistoryUpdate + timedelta(seconds=60):
      self._history.append((now, float(value)))
      self._lastHistoryUpdate = now
  
  def getHistory(self):
    """Return list of last 120 minutes of flow data as dicts with timestamp and value"""
    return [{"timestamp": ts.isoformat(), "value": val} for ts, val in self._history]

class TestWaterflow(BaseWaterflow):
  def __init__(self, logger, config):
    BaseWaterflow.__init__(self, logger, config)
    self.exception = False

  # Can be called multiple times. Make sure to initialize only once
  def start(self):
    if self.exception:
      raise Exception("Test exception in waterflow.start()")

    if self.started:
      return

    self.logger.info("TestWaterflow '%s' started." % self.type)
    self.worker = threading.Thread(target=self.tickerThread, args=())
    self.worker.daemon = True
    self.worker.name = f"WtrFlwTh-{self.type}"
    self.worker.start()
    self.started = True

  def tickerThread(self):
    while True:
      time.sleep(10)
      _ = random.randint(0, 50)
      if _ > 25:
        _ = 0
      self.setLastLiter_1m(_)

class MqttWaterflow(BaseWaterflow):
  def __init__(self, logger, config):
    BaseWaterflow.__init__(self, logger, config)
    self.terminated = False

  # Can be called multiple times. Make sure to initialize only once
  def start(self):
    if self.started:
      return

    self.logger.info("MqttWaterflow '%s' connecting to '%s'..." % (self.type, self.config.hostname))
    try:
      self.mqttClient = self.getMyMqtt()
      self.mqttClient.on_message = self.on_message
      worker = threading.Thread(target=self.mqttLooper, args=())
      worker.daemon = True
      worker.name = f"WtrFlwTh-{self.type}"
      worker.start()
      while not self.mqttClient.is_connected():
        self.logger.info("Waiting for MqttWaterflow connection...")
        time.sleep(1)
      self.logger.info("MqttWaterflow connected: %s" % self.mqttClient.is_connected())
      self.started = True
    except Exception as ex:
      self.logger.error("Error starting MqttWaterflow: %s" % format(ex))

  def getMyMqtt(self):
    mqttClient = client.Client(client.CallbackAPIVersion.VERSION1, self.config.clientname)
    mqttClient.user_data_set(self)
    mqttClient.on_connect = self.on_connect
    mqttClient.on_disconnect = self.on_disconnect
    mqttClient.connect(self.config.hostname)
    return mqttClient

  def on_connect(self, client, userdata, flags, rc):
    if rc == 0:
      self.logger.info("MqttWaterflow connected to MQTT Broker. Subscribing to topic...")
      # Re-subscribe on every connect/reconnect
      self.mqttClient.subscribe(self.config.topic)
      self.logger.info("MqttWaterflow subscribed to topic '%s'" % self.config.topic)
    else:
      self.logger.error("MqttWaterflow failed to connect, return code %d" % rc)

  def on_disconnect(self, client, userdata, rc):
    if rc != 0:
      self.logger.warning("MqttWaterflow connection lost unexpectedly (code: %d). Will attempt reconnection." % rc)
    else:
      self.logger.info("MqttWaterflow disconnected gracefully.")

  def shutdown(self):
    """Gracefully shutdown MQTT connection"""
    self.terminated = True
    if self.mqttClient:
      try:
        self.logger.info("MqttWaterflow shutting down MQTT connection...")
        self.mqttClient.disconnect()
      except Exception as ex:
        self.logger.error("MqttWaterflow error during shutdown: %s" % format(ex))

  def mqttLooper(self):
    self.logger.info("MqttWaterflow '%s' thread started..." % self.type)
    while not self.terminated:
      try:
        self.mqttClient.loop_forever(retry_first_connection=True)
        # If we reach here, loop exited
        if self.terminated:
          break
        self.logger.warning("MqttWaterflow loop exited, reconnecting...")
        time.sleep(5)
      except Exception as ex:
        self.logger.error("MqttWaterflow loop exception: %s. Reconnecting..." % format(ex))
        if self.terminated:
          break
        time.sleep(5)
    self.logger.info("MqttWaterflow '%s' thread terminated" % self.type)

  def on_message(self, client, userdata, msg):
    self.logger.debug("MqttWaterflow received message: '%s' = %s" % (msg.topic, msg.payload))
    try:
      self.setLastLiter_1m(float(msg.payload))
    except Exception as ex:
      self.logger.error("MqttWaterflow '%s' failed to parse payload. Topic '%s' = '%s'. Error message: '%s'" % (self.type, msg.topic, msg.payload, ex.message))

class GpioWaterflow(BaseWaterflow):
  def __init__(self, logger, config):
    BaseWaterflow.__init__(self, logger, config)

  # Can be called multiple times. Make sure to initialize only once
  def start(self):
    raise Exception("Not implemented.")

def waterflowFactory(type, logger, config):
  if type == 'mqtt':
    return MqttWaterflow(logger, config)

  if type == 'gpio':
    return GpioWaterflow(logger, config)

  if type == 'test':
    return TestWaterflow(logger, config)
