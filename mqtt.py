import time
import model
import threading
from paho.mqtt import client

class Mqtt:
  def __init__(self, irrigate):
    self.logger = irrigate.logger
    self.cfg = irrigate.cfg
    self.valves = irrigate.valves
    self.irrigate = irrigate
    self.mqttStarted = False

  def start(self):
    self.logger.info("Connecting to MQTT service '%s'..." % self.cfg.mqttHostName)
    try:
      self.mqttClient = self.getMyMqtt()

      topicPrefix = str(self.cfg.mqttClientName) + "/"
      self.registerTopics(topicPrefix, "queue")
      self.registerTopics(topicPrefix, "suspend")
      self.registerTopics(topicPrefix, "enabled")
      self.registerTopics(topicPrefix, "forceopen")
      self.registerTopics(topicPrefix, "forceclose")
      self.mqttClient.on_message = self.on_message

      worker = threading.Thread(target=self.mqttLooper, args=())
      worker.setDaemon(True)
      worker.start()
      while not self.mqttClient.is_connected():
        self.logger.info("Waiting for MQTT connection...")
        time.sleep(1)
      self.mqttStarted = True

      self.logger.info("MQTT connected: %s" % self.mqttClient.is_connected())
    except Exception as ex:
      self.logger.error("Error starting MQTT: %s" % format(ex))

  def registerTopics(self, topicPrefix, topic):
    topicStr = topicPrefix + topic + "/+/command"
    self.mqttClient.subscribe(topicStr)
    self.logger.info("Topic '%s' registered." % topicStr)

  def getMyMqtt(self):
    mqttClient = client.Client(client.CallbackAPIVersion.VERSION1, self.cfg.mqttClientName)
    mqttClient.user_data_set(self)
    mqttClient.on_connect = self.on_connect
    mqttClient.on_disconnect = self.on_disconnect
    mqttClient.connect(self.cfg.mqttHostName)
    return mqttClient

  def mqttLooper(self):
    self.logger.info("MQTT thread started...")
    while not self.irrigate.terminated:
      try:
        self.mqttClient.loop_forever(retry_first_connection=True)
        # If we reach here, loop exited
        if self.irrigate.terminated:
          break
        self.logger.warning("MQTT loop exited, reconnecting...")
        time.sleep(5)
      except Exception as ex:
        self.logger.error("MQTT loop exception: %s. Reconnecting..." % format(ex))
        if self.irrigate.terminated:
          break
        time.sleep(5)
    self.logger.info("MQTT thread terminated")

  def on_connect(self, client, userdata, flags, rc):
    if rc == 0:
      self.logger.info("Connected to MQTT Broker.")
      self.mqttStarted = True
    else:
      self.logger.error("Failed to connect, return code %d\n" % (rc))

  def on_disconnect(self, client, userdata, rc):
    self.mqttStarted = False
    if rc != 0:
      self.logger.warning("MQTT connection lost unexpectedly (code: %d). Will attempt reconnection." % rc)
    else:
      self.logger.info("MQTT disconnected gracefully.")

  def shutdown(self):
    """Gracefully shutdown MQTT connection"""
    if self.mqttClient:
      try:
        self.logger.info("Shutting down MQTT connection...")
        self.mqttClient.disconnect()
        self.mqttStarted = False
      except Exception as ex:
        self.logger.error("Error during MQTT shutdown: %s" % format(ex))

  def on_message(self, client, userdata, msg):
    self.logger.info("Received message: " + str(msg.topic))
    self.processMessages(msg.topic, msg.payload)

  def publish(self, topic, payload):
    topicPrefix = str(self.cfg.mqttClientName)
    if not topic.startswith("/"):
      topicPrefix = topicPrefix + "/raspi/"
    
    full_topic = topicPrefix + topic
    
    if not self.mqttStarted:
      self.logger.debug("MQTT not connected. Message for topic '%s' not published." % full_topic)
      return False
    
    try:
      result = self.mqttClient.publish(full_topic, payload)
      if result.rc != 0:
        self.logger.warning("MQTT publish failed for topic '%s' with return code %d" % (full_topic, result.rc))
        return False
      
      self.logger.debug("MQTT message published for topic '%s' payload '%s'." % (full_topic, payload))
      return True
      
    except Exception as ex:
      self.logger.error("MQTT publish exception for topic '%s': %s" % (full_topic, format(ex)))
      return False

  def processMessages(self, topic, payload):
    self.logger.debug("MQTT message received for topic '%s' payload '%s'." % (topic, payload))
    try:
      topicParts = topic.split("/")
      valveName = topicParts[2].replace('_', ' ')
      if valveName not in self.valves:
        raise Exception(f"Valve name '{valveName}' does not exist in configuration. Ignoring message.")

      valves = self.valves

      if topicParts[1] == "queue":
        self.irrigate.queueJob(model.Job(valve=valves[valveName], sched=None, duration=float(payload)))
        return

      try:
        if topicParts[1] == "suspend":
          if int(payload) == 0:
            valves[valveName].suspended = False
            self.logger.info("Un-suspended valve '%s' via MQTT command" % valveName)
            return
          elif int(payload) == 1:
            valves[valveName].suspended = True
            self.logger.info("Suspended valve '%s' via MQTT command" % valveName)
            return

        if topicParts[1] == "enabled":
          if int(payload) == 0:
            valves[valveName].enabled = False
            self.logger.info("Disabled valve '%s' via MQTT command" % valveName)
            return
          elif int(payload) == 1:
            valves[valveName].enabled = True
            self.logger.info("Enabled valve '%s' via MQTT command" % valveName)
            return

        if topicParts[1] == "forceopen":
          valves[valveName].open()
          return

        if topicParts[1] == "forceclose":
          valves[valveName].close()
          return
      finally:
        self.irrigate.telemetryValve(valves[valveName])

      self.logger.warning("Invalid payload received for topic %s = '%s'" % (topic, payload))
    except Exception as ex:
      self.logger.error("Error parsing payload received for topic %s = '%s'. Error message: '%s'" % (topic, payload, format(ex)))
