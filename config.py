import copy
import json
import math
import os
import re
import stat
import tempfile
import threading
from types import SimpleNamespace

import pytz
from jsonschema import Draft7Validator, SchemaError, ValidationError

from scheduling import duration_seconds
from sensors.base_sensor import sensorFactory
from valves import valveFactory
from waterflows import waterflowFactory


_ALERT_FLAGS = (
  "leak", "malfunction_no_flow", "irregular_flow", "sensor_error", "system_exit",
  "safety_intervention", "actuation_failure", "monitoring_unavailable",
)
_SCHEDULE_FIELDS = (
  "time_based_on", "fixed_start_time", "offset_minutes", "duration",
  "days", "seasons", "enable_uv_adjustments",
)


def _namespace(value):
  if isinstance(value, dict):
    return SimpleNamespace(**{key: _namespace(item) for key, item in value.items()})
  if isinstance(value, list):
    return [_namespace(item) for item in value]
  return value


def _plain(value):
  if isinstance(value, SimpleNamespace):
    return {key: _plain(item) for key, item in vars(value).items()}
  if isinstance(value, list):
    return [_plain(item) for item in value]
  return copy.deepcopy(value)


class Config:
  def __init__(self, logger, filename, *, valve_factory=None, sensor_factory=None,
               waterflow_factory=None, runtime_lock=None):
    self._configure(logger, filename, valve_factory, sensor_factory,
                    waterflow_factory, runtime_lock)
    try:
      self._initialize(self._read_candidate(self.filename))
    except Exception as exc:
      self.logger.error(f"Failed to load configuration '{self.filename}': {exc}")
      raise

  def _configure(self, logger, filename, valve_factory, sensor_factory,
                 waterflow_factory, runtime_lock):
    self.logger = logger
    self.filename = os.path.abspath(os.fspath(filename))
    self.last_good_filename = self.filename + ".last-good"
    self.runtime_lock = runtime_lock if runtime_lock is not None else threading.RLock()
    self._writer_lock = threading.RLock()
    self._transaction_active = False
    self._valve_factory = valveFactory if valve_factory is None else valve_factory
    self._sensor_factory = sensorFactory if sensor_factory is None else sensor_factory
    self._waterflow_factory = waterflowFactory if waterflow_factory is None else waterflow_factory
    self._injected = {
      "valves": valve_factory is not None,
      "sensors": sensor_factory is not None,
      "waterflow": waterflow_factory is not None,
    }
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.schema.json")
    try:
      with open(schema_path, "r", encoding="utf-8") as stream:
        schema = json.load(stream)
      Draft7Validator.check_schema(schema)
      self._validator = Draft7Validator(schema)
    except (OSError, ValueError, SchemaError) as exc:
      self.logger.error(f"Cannot load bundled configuration schema '{schema_path}': {exc}")
      raise

  @staticmethod
  def _reject_constant(value):
    raise ValueError(f"Configuration numbers must be finite, not {value}")

  def _read_candidate(self, filename):
    with open(filename, "r", encoding="utf-8") as stream:
      data = json.load(stream, parse_constant=self._reject_constant)
    return self.validate_config_schema(data)

  @staticmethod
  def _check_json(value, path="root"):
    if isinstance(value, dict):
      for key, item in value.items():
        if not isinstance(key, str):
          raise ValueError(f"Configuration keys must be strings at '{path}'")
        Config._check_json(item, f"{path}.{key}")
    elif isinstance(value, list):
      for index, item in enumerate(value):
        Config._check_json(item, f"{path}[{index}]")
    elif isinstance(value, float):
      if not math.isfinite(value):
        raise ValueError(f"Configuration number at '{path}' must be finite")
    elif value is not None and not isinstance(value, (str, int, bool)):
      raise ValueError(f"Configuration value at '{path}' is not a JSON value")

  @staticmethod
  def _integers(data, *keys):
    for key in keys:
      value = data.get(key)
      if isinstance(value, float) and value.is_integer():
        data[key] = int(value)

  @classmethod
  def _normalize_windows(cls, windows, uv=False):
    if not isinstance(windows, list):
      return
    for window in windows:
      if isinstance(window, dict):
        window.setdefault("days", [])
        window.setdefault("seasons", [])
        if uv:
          window.setdefault("enable_uv_adjustments", False)
        cls._integers(window, "offset_minutes")

  @classmethod
  def _normalize(cls, data):
    if not isinstance(data, dict):
      return
    cls._integers(data, "max_concurrent_valves", "flow_sensor_pin")
    data.setdefault("sensors", [])
    if isinstance(data["sensors"], list):
      for sensor in data["sensors"]:
        if not isinstance(sensor, dict):
          continue
        sensor.setdefault("uv_adjustments", [])
        precipitation = sensor.setdefault("precipitation", {})
        if isinstance(precipitation, dict):
          precipitation.setdefault("days_to_aggregate", 3)
          precipitation.setdefault("disable_threshold_mm", 1.0)
          cls._integers(precipitation, "days_to_aggregate")
    alerts = data.get("alerts")
    if isinstance(alerts, dict):
      enabled = alerts.setdefault("enabled", {})
      if isinstance(enabled, dict):
        for flag in _ALERT_FLAGS:
          enabled.setdefault(flag, True)
      alerts.setdefault("leak_repeat_minutes", 15)
      alerts.setdefault("irregular_flow_threshold", 2.0)
      cls._normalize_windows(alerts.setdefault("leak_detection_exclusions", []))
      channels = alerts.get("channels", [])
      if isinstance(channels, list):
        for channel in channels:
          if isinstance(channel, dict):
            cls._integers(channel, "user_id")
    valves = data.get("valves", [])
    if isinstance(valves, list):
      for valve in valves:
        if isinstance(valve, dict):
          cls._integers(valve, "gpio_on_pin", "gpio_off_pin")
          cls._normalize_windows(valve.get("schedules"), uv=True)

  @staticmethod
  def _reject_unimplemented(data):
    if not isinstance(data, dict):
      return
    valves = data.get("valves", [])
    if isinstance(valves, list):
      for valve in valves:
        if isinstance(valve, dict):
          if valve.get("type") == "2wire":
            raise ValueError("Valve type '2wire' is not implemented")
          if valve.get("watering_mode") == "volume":
            raise ValueError("Watering mode 'volume' is not implemented")
    waterflow = data.get("waterflow")
    if isinstance(waterflow, dict) and waterflow.get("type") == "gpio":
      raise ValueError("Waterflow type 'gpio' is not implemented")

  def _validate_semantics(self, data):
    try:
      pytz.timezone(data["timezone"])
    except pytz.UnknownTimeZoneError as exc:
      raise ValueError(f"Unknown timezone '{data['timezone']}'") from exc
    for category in ("valves", "sensors"):
      names = [item["name"] for item in data[category]]
      if len(set(names)) != len(names):
        raise ValueError(f"Duplicate names in '{category}'")
      for item in data[category]:
        if item["type"] == "test" and not self._injected[category]:
          raise ValueError(f"Type 'test' in '{category}' requires an explicit injected factory")
    waterflow = data.get("waterflow")
    if waterflow and waterflow["type"] == "test" and not self._injected["waterflow"]:
      raise ValueError("Waterflow type 'test' requires an explicit injected factory")
    sensors = {sensor["name"]: sensor for sensor in data["sensors"]}
    windows = list(data["alerts"]["leak_detection_exclusions"])
    for valve in data["valves"]:
      if "sensor" in valve and valve["sensor"] not in sensors:
        raise ValueError(f"Unknown sensor '{valve['sensor']}' in valve '{valve['name']}'")
      if "sensor" not in valve and any(s["enable_uv_adjustments"] for s in valve["schedules"]):
        raise ValueError(f"Cannot enable UV adjustments without a sensor in valve '{valve['name']}'")
      windows.extend(valve["schedules"])
    for window in windows:
      try:
        duration_seconds(window["duration"])
      except ValueError as exc:
        raise ValueError("Schedule/exclusion duration must be representable as a timedelta greater than zero") from exc
      if window["time_based_on"] == "fixed":
        if re.fullmatch(r"([01]?[0-9]|2[0-3]):[0-5][0-9]", window["fixed_start_time"]) is None:
          raise ValueError("Fixed start times must be valid H:MM or HH:MM times")
    for valve in data["valves"]:
      sensor = sensors.get(valve.get("sensor"))
      if sensor is None:
        continue
      for index, schedule in enumerate(valve["schedules"]):
        if not schedule["enable_uv_adjustments"]:
          continue
        for adjustment_index, adjustment in enumerate(sensor["uv_adjustments"]):
          multiplier = adjustment["multiplier"]
          if multiplier == 0:
            continue
          try:
            duration = float(schedule["duration"]) * float(multiplier)
            if not math.isfinite(duration) or duration <= 0:
              raise ValueError("Adjusted duration is not finite and positive")
            duration_seconds(duration)
          except (OverflowError, ValueError) as exc:
            raise ValueError(
              f"UV-adjusted duration for valve '{valve['name']}', schedule {index}, "
              f"adjustment {adjustment_index} must be finite, positive and representable as a timedelta"
            ) from exc
    valve_names = {valve["name"] for valve in data["valves"]}
    for name in data["alerts"].get("valve_overrides", {}):
      if name not in valve_names:
        raise ValueError(f"Unknown valve '{name}' in alert valve_overrides")

  def validate_config_schema(self, config_data):
    """Return a normalized, fully validated copy without changing the input."""
    try:
      data = copy.deepcopy(config_data)
      self._check_json(data)
      self._normalize(data)
      self._reject_unimplemented(data)
      self._validator.validate(data)
      self._validate_semantics(data)
      return data
    except ValidationError as exc:
      path = " -> ".join(str(part) for part in exc.path) or "root"
      message = f"Configuration validation failed at '{path}': {exc.message}"
      self.logger.error(message)
      raise ValueError(message) from exc
    except Exception as exc:
      self.logger.error(f"Configuration validation failed: {exc}")
      raise

  @staticmethod
  def _globals(cfg):
    return {
      "cfg": cfg,
      "mqttEnabled": cfg.mqtt.enabled,
      "mqttClientName": cfg.mqtt.client_name,
      "mqttHostName": cfg.mqtt.hostname,
      "valvesConcurrency": cfg.max_concurrent_valves,
      "timezone": cfg.timezone,
      "latitude": cfg.location.latitude,
      "longitude": cfg.location.longitude,
      "telemetry": cfg.telemetry.enabled,
      "telemIdleInterval": getattr(cfg.telemetry, "idle_interval", None),
      "telemActiveInterval": getattr(cfg.telemetry, "active_interval", None),
    }

  def _initialize(self, data):
    self._data = data
    self.__dict__.update(self._globals(_namespace(data)))
    self.sensors = self.initSensors()
    self.waterflow = self.initWaterFlows()
    self.valves = self.initValves()
    self.logger.info(f"Configuration loaded from '{self.filename}'")

  def _make_adapter(self, factory, cfg):
    adapter = factory(cfg.type, self.logger, cfg)
    if adapter is None:
      raise ValueError(f"Factory returned no adapter for type '{cfg.type}'")
    if not isinstance(vars(adapter), dict):
      raise TypeError("Configuration adapters must have mutable instance attributes")
    return adapter

  def initSensors(self):
    return {cfg.name: self._make_adapter(self._sensor_factory, cfg) for cfg in self.cfg.sensors}

  def initWaterFlows(self):
    if not hasattr(self.cfg, "waterflow"):
      return None
    return self._make_adapter(self._waterflow_factory, self.cfg.waterflow)

  def initValves(self):
    valves = {}
    for cfg in self.cfg.valves:
      valve = self._make_adapter(self._valve_factory, cfg)
      if hasattr(cfg, "sensor"):
        valve.sensor = self.sensors[cfg.sensor]
      valves[cfg.name] = valve
    return valves

  def getLatLon(self):
    with self.runtime_lock:
      return self.latitude, self.longitude

  def get_data(self):
    """Return a detached snapshot of the last accepted, normalized JSON data."""
    with self.runtime_lock:
      data = self._data
    return copy.deepcopy(data)

  def _validate_runtime_layout(self, data):
    for category in ("valves", "sensors"):
      before = {item["name"]: item["type"] for item in self._data[category]}
      after = {item["name"]: item["type"] for item in data[category]}
      if before != after:
        raise ValueError(f"Changing {category} names, types or membership requires a restart")
    before = self._data.get("waterflow")
    after = data.get("waterflow")
    if (before is None) != (after is None) or (
        before is not None and before["type"] != after["type"]):
      raise ValueError("Changing the waterflow adapter requires a restart")
    old_valves = {item["name"]: item for item in self._data["valves"]}
    for valve in data["valves"]:
      for key in ("gpio_on_pin", "gpio_off_pin", "pulse_duration"):
        if valve.get(key) != old_valves[valve["name"]].get(key):
          raise ValueError(f"Changing valve '{valve['name']}' {key} requires a restart")

  def _prepare_publication(self, data):
    cfg = _namespace(data)
    updates = []
    sensors = {item.name: self.sensors[item.name] for item in cfg.sensors}
    valves = {item.name: self.valves[item.name] for item in cfg.valves}
    for item in cfg.sensors:
      sensor = sensors[item.name]
      fields = {
        "config": item, "enabled": item.enabled, "uv_adjustments": item.uv_adjustments,
        "precip_days": item.precipitation.days_to_aggregate,
        "precip_threshold": item.precipitation.disable_threshold_mm,
      }
      for attribute, key in (("apiKey", "api_key"), ("lat", "latitude"), ("lon", "longitude")):
        if hasattr(sensor, attribute) and hasattr(item, key):
          fields[attribute] = getattr(item, key)
      updates.append((vars(sensor), fields, ()))
    for item in cfg.valves:
      fields = {"config": item, "enabled": item.enabled, "schedules": item.schedules}
      if hasattr(item, "sensor"):
        fields["sensor"] = sensors[item.sensor]
      updates.append((vars(valves[item.name]), fields, () if "sensor" in fields else ("sensor",)))
    if self.waterflow is not None:
      updates.append((vars(self.waterflow), {
        "config": cfg.waterflow, "leakdetection": cfg.waterflow.leakdetection,
      }, ()))
    globals_ = self._globals(cfg)
    globals_["_data"] = data
    return updates, sensors, valves, globals_

  def _publish(self, publication, on_publish=None):
    updates, sensors, valves, globals_ = publication
    with self.runtime_lock:
      for attributes, fields, removed in updates:
        attributes.update(fields)
        for key in removed:
          attributes.pop(key, None)
      for live, ordered in ((self.sensors, sensors), (self.valves, valves)):
        if list(live) != list(ordered):
          live.clear()
          live.update(ordered)
      self.__dict__.update(globals_)
      if self.waterflow is not None and self.waterflow.enabled != self.cfg.waterflow.enabled:
        # The flow setter records an in-memory availability boundary, without I/O.
        self.waterflow.enabled = self.cfg.waterflow.enabled
      if on_publish is not None:
        on_publish()

  def transaction(self, mutator, *, on_publish=None):
    """Mutate a private dict, validate, durably replace, then publish and return a copy.

    Writers serialize independently of runtime_lock. Adapter identity, membership,
    wiring and pulse timing cannot be changed live; those edits require a restart.

    on_publish is a trusted, no-argument callback invoked under runtime_lock after
    durable commit and complete runtime publication. It must be short, perform no
    I/O and not raise. Unexpected callback errors propagate after the commit.
    """
    with self._writer_lock:
      if self._transaction_active:
        raise RuntimeError("Nested configuration transactions are not supported")
      self._transaction_active = True
      try:
        if on_publish is not None and not callable(on_publish):
          raise TypeError("on_publish must be callable or None")
        candidate = copy.deepcopy(self._data)
        mutator(candidate)
        data = self.validate_config_schema(candidate)
        self._validate_runtime_layout(data)
        publication = self._prepare_publication(data)
        result = copy.deepcopy(data)
        self._persist(data, previous=self._data)
        self._publish(publication, on_publish=on_publish)
        self.logger.info(f"Runtime configuration saved to '{self.filename}'")
        return result
      except Exception as exc:
        self.logger.error(f"Configuration transaction failed for '{self.filename}': {exc}")
        raise
      finally:
        self._transaction_active = False

  @staticmethod
  def _serialize(data):
    return (json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")

  def _remove_stage(self, path):
    if path is not None:
      try:
        os.unlink(path)
      except FileNotFoundError:
        pass
      except OSError as exc:
        self.logger.error(f"Cannot remove configuration staging file '{path}': {exc}")

  def _stage_file(self, target, contents, mode):
    descriptor, path = tempfile.mkstemp(
      prefix="." + os.path.basename(target) + ".", suffix=".tmp",
      dir=os.path.dirname(target),
    )
    try:
      with os.fdopen(descriptor, "wb") as stream:
        descriptor = None
        stream.write(contents)
        stream.flush()
        if mode is not None:
          os.chmod(path, mode)
        os.fsync(stream.fileno())
      return path
    except BaseException:
      if descriptor is not None:
        os.close(descriptor)
      self._remove_stage(path)
      raise

  @staticmethod
  def _sync_directory(directory):
    if os.name == "nt":
      return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
      os.fsync(descriptor)
    finally:
      os.close(descriptor)

  def _persist(self, data, previous=None):
    replacements = []
    if previous is not None:
      replacements.append((self.last_good_filename, self._serialize(previous)))
    replacements.append((self.filename, self._serialize(data)))
    records = []
    replaced = []
    try:
      # Stage replacements AND rollback copies before changing either public path.
      for target, contents in replacements:
        original, mode = None, None
        try:
          with open(target, "rb") as stream:
            original = stream.read()
            mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
        except FileNotFoundError:
          pass
        record = {"target": target, "staged": None, "rollback": None}
        records.append(record)
        record["staged"] = self._stage_file(target, contents, mode)
        if original is not None:
          record["rollback"] = self._stage_file(target, original, mode)
      for record in records:
        os.replace(record["staged"], record["target"])
        record["staged"] = None
        replaced.append(record)
      self._sync_directory(os.path.dirname(self.filename))
    except BaseException as exc:
      failures = []
      for record in reversed(replaced):
        try:
          if record["rollback"] is None:
            os.unlink(record["target"])
          else:
            os.replace(record["rollback"], record["target"])
            record["rollback"] = None
        except OSError as rollback_error:
          failures.append(str(rollback_error))
          self.logger.error(
            f"Configuration rollback failed for '{record['target']}'; "
            f"recovery copy: {record['rollback']}: {rollback_error}"
          )
          # Retain the only rollback copy if recovery itself failed.
          record["rollback"] = None
      if replaced:
        try:
          self._sync_directory(os.path.dirname(self.filename))
        except OSError as rollback_error:
          failures.append(str(rollback_error))
      if failures:
        message = "Configuration persistence failed and rollback failed: " + "; ".join(failures)
        self.logger.error(message)
        raise OSError(message) from exc
      raise
    finally:
      for record in records:
        self._remove_stage(record["staged"])
        self._remove_stage(record["rollback"])

  @classmethod
  def recover_last_good(cls, logger, filename, *, valve_factory=None,
                        sensor_factory=None, waterflow_factory=None, runtime_lock=None):
    """Explicit startup recovery from <filename>.last-good; never a silent fallback.

    Validate the backup and durably restore the primary before constructing any
    adapters. The backup is preserved. Return a newly initialized Config.
    """
    config = cls.__new__(cls)
    config._configure(logger, filename, valve_factory, sensor_factory,
                      waterflow_factory, runtime_lock)
    try:
      data = config._read_candidate(config.last_good_filename)
      config._persist(data)
      config._initialize(data)
      logger.warning(f"Recovered configuration from '{config.last_good_filename}'")
      return config
    except Exception as exc:
      logger.error(f"Configuration recovery failed for '{config.filename}': {exc}")
      raise

  def _merge_runtime_config(self, candidate):
    with self.runtime_lock:
      for item in candidate["valves"]:
        valve = self.valves[item["name"]]
        item["enabled"] = valve.enabled
        item["schedules"] = [
          {key: _plain(getattr(schedule, key)) for key in _SCHEDULE_FIELDS if hasattr(schedule, key)}
          for schedule in valve.schedules
        ]
      alerts = self.cfg.alerts
      for flag in _ALERT_FLAGS:
        if hasattr(alerts.enabled, flag):
          candidate["alerts"]["enabled"][flag] = getattr(alerts.enabled, flag)
      for key in ("leak_repeat_minutes", "irregular_flow_threshold"):
        candidate["alerts"][key] = getattr(alerts, key)
      if self.waterflow is not None:
        for key in ("enabled", "leakdetection"):
          candidate["waterflow"][key] = getattr(self.cfg.waterflow, key)
      sensors = {sensor.name: sensor for sensor in self.cfg.sensors}
      for item in candidate["sensors"]:
        sensor = sensors[item["name"]]
        item["enabled"] = sensor.enabled
        for key in ("days_to_aggregate", "disable_threshold_mm"):
          item["precipitation"][key] = getattr(sensor.precipitation, key)
        item["uv_adjustments"] = [
          {key: _plain(getattr(adjustment, key)) for key in ("max_uv_index", "multiplier")}
          for adjustment in sensor.uv_adjustments
        ]

  def save_runtime_config(self):
    """Legacy wrapper; new callers should mutate only transaction candidates."""
    with self._writer_lock:
      try:
        return self.transaction(self._merge_runtime_config)
      except Exception:
        self._publish(self._prepare_publication(self._data))
        raise
