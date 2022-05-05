import time
import threading
from datetime import datetime
from datetime import timedelta
from paho.mqtt import client 

class BaseWaterflow():
  def __init__(self, logger, name, config):
    self.logger = logger
    self.name = name
    self.config = config
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
  def __init__(self, logger, name, config):
    BaseWaterflow.__init__(self, logger, name, config)
    self.exception = False

  # Can be called multiple times. Make sure to initialize only once
  def start(self):
    if self.exception:
      raise Exception("Test exception in waterflow.start()")

    if self.started:
      return

    self.started = True
    self.logger.info("TestWaterflow '%s' started." % self.name)
    self.worker = threading.Thread(target=self.tickerThread, args=())
    self.worker.setDaemon(True)
    self.worker.setName("WtrFlwTh-%s" % self.name)
    self.worker.start()

  def tickerThread(self):
    while True:
      time.sleep(10)
      self._lastLiter_1m = 24

class MqttWaterflow(BaseWaterflow):
  def __init__(self, logger, name, config):
    BaseWaterflow.__init__(self, logger, name, config)

  # Can be called multiple times. Make sure to initialize only once
  def start(self):
    if self.started:
      return

    self.logger.info("MqttWaterflow '%s' connecting to '%s'..." % (self.name, self.config['hostname']))
    try:
      self.mqttClient = self.getMyMqtt()
      self.mqttClient.subscribe(self.config['topic'])
      self.mqttClient.on_message = self.on_message
      worker = threading.Thread(target=self.mqttLooper, args=())
      worker.setDaemon(True)
      worker.setName("WtrFlwTh-%s" % self.name)
      worker.start()
      while not self.mqttClient.is_connected():
        self.logger.info("Waiting for MqttWaterflow connection...")
        time.sleep(1)
      self.logger.info("MqttWaterflow connected: %s" % self.mqttClient.is_connected())
      self.started = True
    except Exception as ex:
      self.logger.error("Error starting MqttWaterflow: %s" % format(ex))

  def getMyMqtt(self):
    mqttClient = client.Client(self.config['clientname'])
    mqttClient.user_data_set(self)
    mqttClient.connect(self.config['hostname'])
    return mqttClient

  def mqttLooper(self):
    self.logger.info("MqttWaterflow '%s' thread started..." % self.name)
    self.mqttClient.loop_forever(retry_first_connection=False)
    self.logger.error("MqttWaterflow '%s' thread loop exited" % self.name)

  def on_message(self, client, userdata, msg):
    self.logger.debug("MqttWaterflow received message: '%s' = %s" % (msg.topic, msg.payload))
    try:
      self.setLastLiter_1m(float(msg.payload))
    except Exception as ex:
      self.logger.error("MqttWaterflow '%s' failed to parse payload. Topic '%s' = '%s'" % (self.name, msg.topic, msg.payload))

def waterflowFactory(type, name, logger, config):
  if type == 'mqtt':
    return MqttWaterflow(logger, name, config)

  if type == 'gpio':
    return GpioWaterflow(logger, name, config)

  if type == 'test':
    return TestWaterflow(logger, name, config)
