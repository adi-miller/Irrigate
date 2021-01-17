import yaml
import model
import sensors.sensors
from sensors.sensors import sensorFactory
from valves import valveFactory

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

    try:
      self.sensors = self.initSensors()
      self.schedules = self.initSchedules()
      self.valves = self.initValves(self.schedules)
    except Exception as ex:
      logger.error("Failed to initialize configuration with error message '%s'. Aborting." % format(ex))
      raise


  def getLatLon(self):
    return self.latitude, self.longitude

  def initValves(self, schedules):
    valves = {}

    for valve in self.cfg['valves']:
      try:
        valveYaml = self.cfg['valves'][valve]
        valveType = valveYaml['type']
        valveObj = model.Valve(valve, valveFactory(valveType, self.logger, valveYaml))
        valveObj.enabled = valveYaml['enabled']
        for sched in valveYaml['schedules']:
          valveObj.schedules[sched] = schedules[sched]
        valves[valve] = valveObj
      except KeyError as ex:
        self.logger.error("Mandatory configuration %s missing in valve '%s'." % (format(ex), valve))
        raise
    
    return valves

  def initSensors(self):
    sensors = {}

    for sensor in self.cfg['sensors']:
      try:
        sensorYaml = self.cfg['sensors'][sensor]
        sensorType = sensorYaml['type']
        sensorObj = model.Sensor(sensorType, sensorFactory(sensorType, self.logger, sensorYaml))
        sensorObj.enabled = sensorYaml['enabled']
        sensors[sensor] = sensorObj
      except KeyError as ex:
        self.logger.error("Mandatory configuration %s missing in sensor '%s'." % (format(ex), sensor))
        raise

    return sensors

  def initSchedules(self):
    scheds = {}

    for sched in self.cfg['schedules']:
      try:
        schedYaml = self.cfg['schedules'][sched]
        aType = schedYaml['type']
        aStart = schedYaml['start']
        aDuration = schedYaml['duration']
        aDays = schedYaml['days']
        aSeasons = schedYaml['seasons']
        schedObj = model.Schedule(sched, aType, aStart, aDuration, aDays, aSeasons)
        if 'sensor' in schedYaml:
          if schedYaml['sensor'] in self.sensors:
            schedObj.sensor = self.sensors[schedYaml['sensor']]
          else:
            self.logger.warning("Sensor type '%s' which is specified in schedules '%s' was not found in sensors section." % (schedYaml['sensor'], sched))
        scheds[sched] = schedObj
      except KeyError as ex:
        self.logger.error("Mandatory configuration %s missing in schedule '%s'." % (format(ex), sched))
        raise

    return scheds
