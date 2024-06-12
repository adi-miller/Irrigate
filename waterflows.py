import time
import threading
from datetime import datetime
from datetime import timedelta
from paho.mqtt import client

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

  def lastLiter_1m(self):
    if datetime.now() > self._lastupdate + timedelta(0, 60):
      return 0

    return self._lastLiter_1m

  def setLastLiter_1m(self, value):
    self._lastLiter_1m = value
    self._lastupdate = datetime.now()

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
    self.worker.setDaemon(True)
    self.worker.setName("WtrFlwTh-%s" % self.type)
    self.worker.start()
    self.started = True

  def tickerThread(self):
    while True:
      time.sleep(10)
      self._lastLiter_1m = 24

class MqttWaterflow(BaseWaterflow):
  def __init__(self, logger, config):
    BaseWaterflow.__init__(self, logger, config)

  # Can be called multiple times. Make sure to initialize only once
  def start(self):
    if self.started:
      return

    self.logger.info("MqttWaterflow '%s' connecting to '%s'..." % (self.type, self.config.hostname))
    try:
      self.mqttClient = self.getMyMqtt()
      self.mqttClient.subscribe(self.config.topic)
      self.mqttClient.on_message = self.on_message
      worker = threading.Thread(target=self.mqttLooper, args=())
      worker.setDaemon(True)
      worker.setName("WtrFlwTh-%s" % self.type)
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
    mqttClient.connect(self.config.hostname)
    return mqttClient

  def mqttLooper(self):
    self.logger.info("MqttWaterflow '%s' thread started..." % self.type)
    self.mqttClient.loop_forever(retry_first_connection=False)
    self.logger.error("MqttWaterflow '%s' thread loop exited" % self.type)

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
