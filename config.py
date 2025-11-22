import json
import os
import model
from valves import valveFactory
from types import SimpleNamespace
from sensors.base_sensor import sensorFactory
from waterflows import waterflowFactory
from jsonschema import validate, ValidationError, SchemaError

class Config:
  def __init__(self, logger, filename):
    self.logger = logger
    self.filename = filename  # Store filename for later saving
    
    # Load configuration file
    with open(filename, 'r') as stream:
      try:
        config_data = json.loads(stream.read())
      except Exception as ex:
        self.logger.exception(ex)
        print(ex)
        raise

    # Validate configuration against JSON schema
    self.validate_config_schema(config_data)
    
    # Convert to SimpleNamespace after validation
    self.cfg = json.loads(json.dumps(config_data), object_hook=lambda d: SimpleNamespace(**d))

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

  def validate_config_schema(self, config_data):
    # Load the schema file
    schema_path = os.path.join(os.path.dirname(self.filename), 'config.schema.json')
    if not os.path.exists(schema_path):
      self.logger.warning(f"Schema file not found at {schema_path} - skipping schema validation")
      return
    
    try:
      with open(schema_path, 'r') as schema_file:
        schema = json.load(schema_file)
      
      # Validate against schema
      validate(instance=config_data, schema=schema)
      self.logger.info("Configuration validation passed successfully")
      
    except ValidationError as e:
      # Format validation error message
      error_path = ' -> '.join(str(p) for p in e.path) if e.path else 'root'
      error_msg = f"Configuration validation failed at '{error_path}': {e.message}"
      self.logger.error(error_msg)
      raise ValueError(error_msg)
    
    except SchemaError as e:
      self.logger.error(f"Invalid schema file: {e.message}")
      raise ValueError(f"Invalid schema file: {e.message}")
    
    except Exception as e:
      self.logger.error(f"Error during schema validation: {str(e)}")
      raise

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
      
      # Update alerts configuration if it exists
      if hasattr(self.cfg, 'alerts') and 'alerts' in config_data:
        alerts_cfg = self.cfg.alerts
        
        # Update enabled flags
        if hasattr(alerts_cfg, 'enabled') and 'enabled' in config_data['alerts']:
          config_data['alerts']['enabled'] = {
            'leak': alerts_cfg.enabled.leak,
            'malfunction_no_flow': alerts_cfg.enabled.malfunction_no_flow,
            'irregular_flow': alerts_cfg.enabled.irregular_flow,
            'sensor_error': alerts_cfg.enabled.sensor_error,
            'system_exit': alerts_cfg.enabled.system_exit,
          }
        
        # Update settings
        if hasattr(alerts_cfg, 'leak_repeat_minutes'):
          config_data['alerts']['leak_repeat_minutes'] = alerts_cfg.leak_repeat_minutes
        if hasattr(alerts_cfg, 'irregular_flow_threshold'):
          config_data['alerts']['irregular_flow_threshold'] = alerts_cfg.irregular_flow_threshold
      
      # Update waterflow configuration if it exists
      if hasattr(self.cfg, 'waterflow') and 'waterflow' in config_data:
        waterflow_cfg = self.cfg.waterflow
        
        # Update waterflow settings
        if hasattr(waterflow_cfg, 'enabled'):
          config_data['waterflow']['enabled'] = waterflow_cfg.enabled
        if hasattr(waterflow_cfg, 'leakdetection'):
          config_data['waterflow']['leakdetection'] = waterflow_cfg.leakdetection
      
      # Update sensors configuration if it exists
      if hasattr(self.cfg, 'sensors') and 'sensors' in config_data:
        for sensor_cfg in self.cfg.sensors:
          # Find matching sensor in config_data
          for sensor_data in config_data['sensors']:
            if sensor_data['name'] == sensor_cfg.name:
              # Update sensor-specific settings
              if hasattr(sensor_cfg, 'precipitation'):
                if 'precipitation' not in sensor_data:
                  sensor_data['precipitation'] = {}
                if hasattr(sensor_cfg.precipitation, 'days_to_aggregate'):
                  sensor_data['precipitation']['days_to_aggregate'] = sensor_cfg.precipitation.days_to_aggregate
                if hasattr(sensor_cfg.precipitation, 'disable_threshold_mm'):
                  sensor_data['precipitation']['disable_threshold_mm'] = sensor_cfg.precipitation.disable_threshold_mm
              break
      
      # Write the updated config back to file with nice formatting
      with open(self.filename, 'w') as f:
        json.dump(config_data, f, indent=2)
      
      self.logger.info(f"Runtime configuration saved to '{self.filename}'")
      
    except Exception as ex:
      self.logger.error(f"Error saving runtime configuration: {ex}")
      raise
