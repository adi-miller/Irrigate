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
    self.mqttClient = self.getMyMqtt()

    topicPrefix = str(self.cfg.mqttClientName) + "/"
    self.mqttClient.subscribe(topicPrefix + "open/+/command")
    self.mqttClient.subscribe(topicPrefix + "suspend/+/command")
    self.mqttClient.subscribe(topicPrefix + "enabled/+/command")
    self.registerTopics(topicPrefix, "open")
    self.mqttClient.on_message = self.on_message

    worker = threading.Thread(target=self.mqttLooper, args=())
    worker.setDaemon(True)
    worker.start()
    self.mqttStarted = True
    self.logger.info("MQTT thread '%s' started." % worker.getName())
    while not self.mqttClient.is_connected():
      self.logger.info("Waiting for MQTT connection...")
      time.sleep(1)

    self.logger.info("MQTT connected: %s" % self.mqttClient.is_connected())

  def registerTopics(self, topicPrefix, topic):
    topicStr = topicPrefix + topic + "/+/command"
    self.mqttClient.subscribe(topicStr)
    self.logger.info("Topic '%s' registered." % topicStr)

  def getMyMqtt(self):
    mqttClient = client.Client(self.cfg.mqttClientName)
    mqttClient.user_data_set(self)
    mqttClient.on_connect = self.on_connect
    mqttClient.connect(self.cfg.mqttHostName)
    return mqttClient

  def mqttLooper(self):
    self.logger.info("MQTT thread started...")
    self.mqttClient.loop_forever(retry_first_connection=False)
    self.logger.error("MQTT thread loop exited")

  def on_connect(self, client, userdata, flags, rc):
    if rc == 0:
      self.logger.info("Connected to MQTT Broker.")
    else:
      self.logger.error("Failed to connect, return code %d\n" % (rc))

  def on_message(self, client, userdata, msg):
    self.logger.debug("Received message: " + str(msg.topic))
    self.processMessages(msg.topic, msg.payload)

  def publish(self, topic, payload):
    topicPrefix = str(self.cfg.mqttClientName)
    if not topic.startswith("/"):
      topicPrefix = topicPrefix + "/raspi/"
    if self.mqttStarted:
      self.mqttClient.publish(topicPrefix + topic, payload)
      self.logger.debug("MQTT message published for topic '%s' payload '%s'." % (topicPrefix + topic, payload))
    else:
      self.logger.debug("MQTT disabled. Message for topic '%s' payload '%s' not published." % (topicPrefix + topic, payload))

  def processMessages(self, topic, payload):
    self.logger.debug("MQTT message received for topic '%s' payload '%s'." % (topic, payload))
    try:
      topicParts = topic.split("/")
      valveName = topicParts[2]

      valves = self.valves

      if topicParts[1] == "open":
        self.irrigate.queueJob(model.Job(valve = valves[valveName], sched = None, duration = float(payload)))
        return

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

      self.logger.warning("Invalid payload received for topic %s = '%s'" % (topic, payload))
    except Exception as ex:
      self.logger.error("Error parsing payload received for topic %s = '%s'" % (topic, payload))
