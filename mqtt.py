import model
import threading 
from paho.mqtt import client 
from paho.mqtt import subscribe 
import time
class MyUserData:
  logger = None
  q = None
  valves = None
  queueJob = None

  def __init__(self, logger, q, valves, queueJob):
    self.logger = logger
    self.q = q
    self.valves = valves
    self.queueJob = queueJob

def initMqtt(cfg, logger, q, queueJob):
  myUserData = MyUserData(logger, q, cfg.valves, queueJob)

  logger.info("Connecting to MQTT service '%s'..." % cfg.mqttHostName)
  mqttClient = getMyMqtt(cfg, myUserData)

  topicPrefix = str(cfg.mqttClientName) + "/"
  mqttClient.subscribe(topicPrefix + "open/+/command")
  mqttClient.subscribe(topicPrefix + "suspend/+/command")
  mqttClient.subscribe(topicPrefix + "enabled/+/command")
  mqttClient.on_message = on_message

  worker = threading.Thread(target=mqttLooper, args=(mqttClient, logger))
  worker.setDaemon(True)
  worker.start()
  logger.info("MQTT thread '%s' started." % worker.getName())
  while not mqttClient.is_connected():
    logger.info("Waiting for MQTT connection...")
    time.sleep(1)

  logger.info("MQTT connected: %s" % mqttClient.is_connected())


def getMyMqtt(cfg, myUserData):
  mqttClient = client.Client(cfg.mqttClientName)
  mqttClient.user_data_set(myUserData)
  mqttClient.on_connect = on_connect
  mqttClient.connect(cfg.mqttHostName)
  return mqttClient

def mqttLooper(mqttClient, logger):
  logger.info("MQTT thread started...")
  mqttClient.loop_forever(retry_first_connection=False)
  logger.error("MQTT thread loop exited")

def on_connect(client, userdata, flags, rc):
  logger = userdata.logger
  if rc == 0:
    logger.info("Connected to MQTT Broker.")
  else:
    logger.error("Failed to connect, return code %d\n" % (rc))

def on_message(client, userdata, msg):
  logger = userdata.logger
  logger.debug("Received message: " + str(msg.topic))
  q = userdata.q
  valves = userdata.valves

  processMessages(logger, valves, q, userdata.queueJob, msg.topic, msg.payload)

def processMessages(logger, valves, q, queueJob, topic, payload):
  topicParts = topic.split("/")
  valveName = topicParts[2]

  if topicParts[1] == "open":
    queueJob(model.Job(valve = valves[valveName], duration = int(payload)))
  if topicParts[1] == "suspend":
    if int(payload) == 0:
      valves[valveName].suspended = False
    elif int(payload) == 1:
      valves[valveName].suspended = True
    else:
      logger.warning("Invalid payload received in topic %s = '%s'" % (topic, payload))
  if topicParts[1] == "enabled":
    if int(payload) == 0:
      valves[valveName].enabled = False
    elif int(payload) == 1:
      valves[valveName].enabled = True
    else:
      logger.warning("Invalid payload received in topic %s = '%s'" % (topic, payload))
