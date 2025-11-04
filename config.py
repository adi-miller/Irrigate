import json
import model
from valves import valveFactory
from types import SimpleNamespace
from sensors.base_sensor import sensorFactory
from waterflows import waterflowFactory

class Config:
  def __init__(self, logger, filename):
    self.logger = logger
    self.filename = filename  # Store filename for later saving
    with open(filename, 'r') as stream:
      try:
        self.cfg = json.loads(stream.read(), object_hook=lambda d: SimpleNamespace(**d))
      except Exception as ex:
        self.logger.exception(ex)
        print(ex)

    try:
      self.mqttEnabled = self.cfg.mqtt.enabled
      self.mqttClientName = self.cfg.mqtt.client_name
      self.mqttHostName = self.cfg.mqtt.hostname
      self.valvesConcurrency = self.cfg.max_concurrent_valves
      self.timezone = self.cfg.timezone
      self.latitude = self.cfg.location.latitude
      self.longitude = self.cfg.location.longitude
      self.telemetry = self.cfg.telemetry.enabled
      if self.telemetry:
        self.telemIdleInterval = self.cfg.telemetry.idle_interval
        self.telemActiveInterval = self.cfg.telemetry.active_interval

    except AttributeError as ex:
      logger.error(f"Error reading configuration '{filename}': {ex}. Aborting.")
      raise

    try:
      self.sensors = self.initSensors()
      self.waterflow = self.initWaterFlows()
      self.valves = self.initValves()
    except Exception as ex:
      logger.error("Failed to initialize configuration with error message '%s'. Aborting." % format(ex))
      raise

  def getLatLon(self):
    return self.latitude, self.longitude

  def initValves(self):
    valves = {}

    for _valve_cfg in self.cfg.valves:
      try:
        valveType = _valve_cfg.type
        valveObj = valveFactory(valveType, self.logger, _valve_cfg)
        if hasattr(_valve_cfg, 'sensor'):
          valveObj.sensor = self.sensors[_valve_cfg.sensor]
        else:
          for _schedule in _valve_cfg.schedules:
            if _schedule.enable_uv_adjustments:
              raise Exception(f"Cannot enable UV adjustments without a sensor in valve '{_valve_cfg.name}' in schedule[{_valve_cfg.schedules.index(_schedule)}]")
        if _valve_cfg.name in valves:
          raise Exception(f"Valve name already exists: {_valve_cfg.name}")
        valves[_valve_cfg.name] = valveObj
      except Exception as ex:
        self.logger.error(f"Error initializing valve '{_valve_cfg.name if hasattr(_valve_cfg, 'name') else 'unnamed'}': {ex}. Aborting.")
        raise

    return valves

  def initSensors(self):
    sensors = {}

    for _sensor_cfg in self.cfg.sensors:
      try:
        sensorObj = sensorFactory(_sensor_cfg.type, self.logger, _sensor_cfg)
        sensors[_sensor_cfg.name] = sensorObj
      except Exception as ex:
        self.logger.error(f"Mandatory configuration is missing in sensor '{_sensor_cfg.name}'. Error: {format(ex)}.")
        raise

    return sensors

  def initWaterFlows(self):
    _waterflow_cfg = self.cfg.waterflow if hasattr(self.cfg, 'waterflow') else None
    if _waterflow_cfg is None:
      return None
    return waterflowFactory(_waterflow_cfg.type, self.logger, _waterflow_cfg)

  def save_runtime_config(self):
    """Save runtime-editable configuration back to the config file.
    
    This method updates valve schedules and enabled flags - all other
    config values remain unchanged from the file.
    """
    try:
      # Read the current config file to preserve formatting and all other settings
      with open(self.filename, 'r') as f:
        config_data = json.load(f)
      
      # Update only the runtime-editable fields for each valve
      for valve_name, valve_obj in self.valves.items():
        # Find the matching valve in the config data
        for valve_cfg in config_data['valves']:
          if valve_cfg['name'] == valve_name:
            # Update enabled flag
            valve_cfg['enabled'] = valve_obj.enabled
            
            # Rebuild the schedules array completely
            new_schedules = []
            for schedule in valve_obj.schedules:
              sched_dict = {}
              
              # Add time_based_on first
              if hasattr(schedule, 'time_based_on'):
                sched_dict['time_based_on'] = schedule.time_based_on
                
                # Add appropriate time fields based on time_based_on
                if schedule.time_based_on == 'fixed':
                  if hasattr(schedule, 'fixed_start_time'):
                    sched_dict['fixed_start_time'] = schedule.fixed_start_time
                  # Do NOT include offset_minutes for fixed time
                else:  # sunrise or sunset
                  if hasattr(schedule, 'offset_minutes'):
                    sched_dict['offset_minutes'] = schedule.offset_minutes
                  # Do NOT include fixed_start_time for sunrise/sunset
              
              # Add other schedule fields
              if hasattr(schedule, 'duration'):
                sched_dict['duration'] = schedule.duration
              if hasattr(schedule, 'seasons') and schedule.seasons:
                sched_dict['seasons'] = schedule.seasons
              if hasattr(schedule, 'days') and schedule.days:
                sched_dict['days'] = schedule.days
              if hasattr(schedule, 'enable_uv_adjustments'):
                sched_dict['enable_uv_adjustments'] = schedule.enable_uv_adjustments
              
              new_schedules.append(sched_dict)
            
            # Replace the schedules array
            valve_cfg['schedules'] = new_schedules
            break
      
      # Write the updated config back to file with nice formatting
      with open(self.filename, 'w') as f:
        json.dump(config_data, f, indent=2)
      
      self.logger.info(f"Runtime configuration saved to '{self.filename}'")
      
    except Exception as ex:
      self.logger.error(f"Error saving runtime configuration: {ex}")
      raise
