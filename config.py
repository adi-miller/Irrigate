import yaml
import datetime
from datetime import datetime, timedelta
import calendar
import pytz
import model
from sensors import sensorFactory

class Config:
  def __init__(self, logger, filename):
    self.logger = logger
    with open(filename, 'r') as stream:
      try:
        self.cfg = yaml.safe_load(stream)
      except yaml.YAMLError as ex:
        self.logger.exception(ex)
        print(ex)

    try:
      self.mqttEnabled = self.cfg['mqtt']['enabled']
      self.mqttClientName = self.cfg['mqtt']['clientname']
      self.mqttHostName = self.cfg['mqtt']['hostname']
      self.valvesConcurrency = self.cfg['general']['valvesConcurrency']
      self.timezone = self.cfg['general']['timezone']
      self.latitude = self.cfg['general']['latitude']
      self.longitude = self.cfg['general']['longitude']
      self.telemetry = self.cfg['telemetry']['enabled']
      self.telemetryInterval = self.cfg['telemetry']['idleinterval']
    except KeyError as ex:
      logger.error("Mandatory configuration value '%s' missing." % format(ex))
      raise

    self.sensors = self.initSensors()
    self.schedules = self.initSchedules()
    self.valves = self.initValves(self.schedules)

  def getLatLon(self):
    return self.latitude, self.longitude

  def initValves(self, schedules):
    valves = {}

    for valve in self.cfg['valves']:
      valveYaml = self.cfg['valves'][valve]
      valveObj = model.Valve(valve)
      valveObj.enabled = valveYaml['enabled']
      for sched in valveYaml['schedules']:
        valveObj.schedules[sched] = schedules[sched]
      valves[valve] = valveObj
    
    return valves

  def initSensors(self):
    sensors = {}

    for sensor in self.cfg['sensors']:
      sensorYaml = self.cfg['sensors'][sensor]
      sensorType = sensorYaml['type']
      sensorObj = model.Sensor(sensorType, sensorFactory(sensorType))
      sensorObj.enabled = sensorYaml['enabled']
      sensors[sensor] = sensorObj

    return sensors

  def initSchedules(self):
    scheds = {}

    for sched in self.cfg['schedules']:
      schedYaml = self.cfg['schedules'][sched]
      aType = schedYaml['type']
      aStart = schedYaml['start']
      aDuration = schedYaml['duration']
      aDays = schedYaml['days']
      schedObj = model.Schedule(sched, aType, aStart, aDuration, aDays)
      if 'sensor' in schedYaml:
        schedObj.sensor = self.sensors[schedYaml['sensor']]
      scheds[sched] = schedObj

    return scheds

