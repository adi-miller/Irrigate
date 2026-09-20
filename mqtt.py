import threading

from paho.mqtt import client

from model import Job


def stop_client(mqtt_client, logger, timeout=2):
  errors = []

  def stop():
    try:
      mqtt_client.disconnect()
      mqtt_client.loop_stop()
    except Exception as error:
      errors.append(error)
      logger.exception("MQTT shutdown failed")

  worker = threading.Thread(target=stop, name="MqttStop", daemon=True)
  worker.start()
  worker.join(timeout)
  if worker.is_alive():
    logger.error("MQTT transport did not stop within the shutdown bound")
  return not worker.is_alive() and not errors


class Mqtt:
  def __init__(self, irrigate, client_factory=None):
    self.irrigate = irrigate
    self.logger = irrigate.logger
    self.cfg = irrigate.cfg
    self.valves = irrigate.valves
    self.client_factory = client_factory
    self.mqttStarted = False
    self.mqttClient = None
    self.topicPrefix = str(self.cfg.mqttClientName) + "/"
    self.last_error = None

  def start(self):
    if self.mqttClient is not None:
      return
    try:
      self.mqttClient = (self.client_factory() if self.client_factory else
                         client.Client(client.CallbackAPIVersion.VERSION1, self.cfg.mqttClientName))
      self.mqttClient.on_connect = self.on_connect
      self.mqttClient.on_disconnect = self.on_disconnect
      self.mqttClient.on_message = self.on_message
      self.mqttClient.reconnect_delay_set(min_delay=1, max_delay=30)
      self.mqttClient.connect_async(self.cfg.mqttHostName)
      result = self.mqttClient.loop_start()
      if result not in (None, 0):
        raise RuntimeError("MQTT loop failed to start: %s" % result)
    except Exception:
      self.last_error = "MQTT startup failed"
      self.logger.exception(self.last_error)
      self.shutdown()
      raise

  def registerTopics(self, topicPrefix, topic):
    self.mqttClient.subscribe(topicPrefix + topic + "/+/command")

  def on_connect(self, mqtt_client, userdata, flags, rc):
    self.mqttStarted = rc == 0
    if self.mqttStarted:
      self.last_error = None
      for topic in ("queue", "enabled", "forceopen", "forceclose"):
        self.registerTopics(self.topicPrefix, topic)
      self.irrigate.reset_telemetry_cursor()
    else:
      self.last_error = "MQTT connection rejected: %s" % rc
      self.logger.error(self.last_error)

  def on_disconnect(self, mqtt_client, userdata, rc):
    self.mqttStarted = False
    if not self.irrigate.terminated:
      self.logger.warning("MQTT disconnected (code %s)", rc)

  def on_message(self, mqtt_client, userdata, msg):
    self.irrigate.submit_mqtt(msg.topic, msg.payload)

  def shutdown(self, timeout=2):
    self.mqttStarted = False
    if self.mqttClient is None:
      return True
    return stop_client(self.mqttClient, self.logger, timeout)

  def publish(self, topic, payload):
    full_topic = str(self.cfg.mqttClientName) + ("" if topic.startswith("/") else "/raspi/") + topic
    if not self.mqttStarted or self.mqttClient is None:
      return False
    try:
      result = self.mqttClient.publish(full_topic, payload)
      if result.rc != 0:
        self.logger.warning("MQTT publish failed for '%s' (code %s)", full_topic, result.rc)
        return False
      return True
    except Exception:
      self.logger.exception("MQTT publish failed for '%s'", full_topic)
      return False

  def processMessages(self, topic, payload, *, command_sequence=None):
    from controller import ControlError, duration_minutes
    try:
      parts = topic.split("/")
      if len(parts) != 4 or parts[3] != "command":
        raise ValueError("Invalid command topic")
      name = parts[2].replace("_", " ")
      if name not in self.valves:
        raise ValueError("Unknown valve")
      action = parts[1]
      if action == "queue":
        duration = duration_minutes(payload)
        self.irrigate.queueJob(Job(self.valves[name], duration, None))
      elif action == "enabled":
        if isinstance(payload, bool):
          raise ValueError("Enabled payload must be 0 or 1")
        value = int(payload)
        if value not in (0, 1):
          raise ValueError("Enabled payload must be 0 or 1")
        self.irrigate.controller.set_enabled(name, value == 1)
      elif action == "forceopen":
        duration = None if payload in (b"", "") else duration_minutes(payload, manual=True)
        if command_sequence is None:
          self.irrigate.controller.start_manual(name, duration)
        else:
          self.irrigate.controller.start_manual(name, duration, command_sequence=command_sequence)
      elif action == "forceclose":
        if command_sequence is None:
          self.irrigate.controller.stop(name)
        else:
          self.irrigate.controller.stop(name, command_sequence=command_sequence)
      else:
        raise ValueError("Unknown command")
      return True
    except (ControlError, ValueError, TypeError, OverflowError) as error:
      self.logger.error("MQTT command rejected on '%s': %s", topic, error)
      return False
    except Exception:
      self.logger.exception("MQTT command failed on '%s'", topic)
      return False
