import argparse
import logging
import math
import queue
import signal
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytz

import config
from alerts import AlertManager, AlertType
from clock import SystemClock
from controller import ControlError, ValveController
from model import Job
from mqtt import Mqtt
from runtime_logging import AsyncLogHandler, CheckedFileHandler
from scheduling import adjusted_duration, schedule_time, season_for, should_run
from sensors.base_sensor import TestSensor
from sensors.openweathermap_sensor import OpenWeatherMapSensor
from valves import TestValve, ThreeWireValve
from waterflows import MqttWaterflow, TestWaterflow


class OfflineChannel:
  def __init__(self):
    self.sent = []

  def send(self, alert):
    self.sent.append(alert)
    return True


class Irrigate:
  def __init__(self, configFilename, *, offline=False, clock=None, logger=None,
               valve_factory=None, sensor_factory=None, waterflow_factory=None,
               channel_factory=None, data_directory=None):
    self.offline = offline
    self.clock = clock or SystemClock()
    self._async_log = None
    self.logger = logger or self.getLogger(None if offline else Path(configFilename).parent / "log.txt")
    self._temporary = tempfile.TemporaryDirectory(prefix="irrigate-offline-") if offline and data_directory is None else None
    self._stop = threading.Event()
    self._shutdown_lock = threading.Lock()
    self._close_lock = threading.Lock()
    self._close_thread = None
    self._close_result = None
    self._heartbeats = {}
    self._state_lock = threading.RLock()
    self._started = False
    self._shutdown_done = False
    self._shutdown_result = None
    self._threads = []
    self._intervalDict = {}
    self._scheduled = {}
    self._baseline_date = None
    self._sensor_cursors = {}
    self._waterflow_reading_revision = 0
    self._mqtt_generation = 0
    self._status = None
    self._tempStatus = {}
    self._lastAllClosed = None
    self._fatal_error = None
    self.terminated = False
    self._mqtt_commands = queue.PriorityQueue(maxsize=128)
    self._mqtt_sequence = 0
    self._mqtt_stops = {}
    self._mqtt_cancelled_through = {}
    self._mqtt_lock = threading.Lock()
    self.cfg = config.Config(
      self.logger, configFilename,
      valve_factory=valve_factory or self._valve_factory,
      sensor_factory=sensor_factory or self._sensor_factory,
      waterflow_factory=waterflow_factory or self._flow_factory,
    )
    if clock is None:
      self.clock.timezone = pytz.timezone(self.cfg.timezone)
    self.startTime = self.clock.now().replace(tzinfo=None)
    self._start_mono = self.clock.monotonic()
    self.valves, self.sensors, self.waterflow = self.cfg.valves, self.cfg.sensors, self.cfg.waterflow
    from valve_metrics import MetricsStore
    directory = (data_directory or (self._temporary.name if self._temporary else
                                    Path(configFilename).parent / "data"))
    self.metrics = MetricsStore(directory, self.logger, clock=self.clock)
    if offline and channel_factory is None:
      channel_factory = lambda logger, cfg: OfflineChannel()
    self.alerts = AlertManager(self.logger, self.cfg, self, clock=self.clock, channel_factory=channel_factory)
    if offline:
      from alert_channels.millerbot import MillerBotChannel
      if any(isinstance(channel, MillerBotChannel) for channel in self.alerts.channels):
        raise ValueError("Offline composition cannot use a real HTTP alert channel")
    self.mqtt = Mqtt(self)
    self.controller = ValveController(
      self.valves, self.cfg.valvesConcurrency, self.clock, self.logger,
      self.alerts, self.waterflow, self.metrics, on_stop=self._cancel_pending_opens,
      is_open_current=self._mqtt_open_current,
    )
    self.cfg.runtime_lock = self.controller.lock
    self.q = self.controller.q
    self.workers = []
    self.timer = None
    self.api_server = None

  def getLogger(self, path=None):
    logger = logging.getLogger("Irrigate.%s" % id(self))
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    sinks = [handler]
    if path is not None:
      handler = CheckedFileHandler(path, mode="a", encoding="utf-8", delay=True)
      handler.setFormatter(formatter)
      sinks.append(handler)
    self._async_log = AsyncLogHandler(sinks)
    logger.addHandler(self._async_log)
    return logger

  def _valve_factory(self, type, logger, cfg):
    if self.offline:
      return TestValve(logger, cfg, self.clock)
    if type != "3wire":
      raise ValueError("Unsupported production valve type: %s" % type)
    return ThreeWireValve(logger, cfg, clock=self.clock)

  def _sensor_factory(self, type, logger, cfg):
    if self.offline:
      return TestSensor(logger, cfg, self.clock)
    if type != "openweathermap":
      raise ValueError("Unsupported production sensor type: %s" % type)
    return OpenWeatherMapSensor(logger, cfg, clock=self.clock)

  def _flow_factory(self, type, logger, cfg):
    if self.offline:
      return TestWaterflow(logger, cfg, self.clock)
    if type != "mqtt":
      raise ValueError("Unsupported production waterflow type: %s" % type)
    return MqttWaterflow(logger, cfg, clock=self.clock)

  def start(self, test=None, *, background=True):
    if self._started:
      return self.controller.ready
    if test is True and not self.offline:
      raise ValueError("Test execution requires explicit offline=True composition")
    if not background and not self.offline:
      raise ValueError("Step-driven execution is restricted to explicit offline composition")
    if self.offline and any(not getattr(valve, "offline_safe", False) for valve in self.valves.values()):
      raise ValueError("Offline composition requires explicitly injected safe valve drivers")
    if self._async_log:
      self._async_log.start()
    self._started = True
    ready = self.controller.reconcile_startup()
    self.metrics.load()
    self.controller.restore_daily()
    self.controller.prepare_daily_export()
    self.metrics.flush()
    self._reload_baselines()
    self._baseline_date = self.clock.now().date()
    for sensor in self.sensors.values():
      if sensor.enabled:
        if self.offline and isinstance(sensor, OpenWeatherMapSensor):
          self.logger.info("Offline mode does not start an HTTP weather worker")
          continue
        try:
          sensor.start()
        except Exception:
          self.logger.exception("Sensor '%s' startup failed", sensor.name)
    if self.waterflow and self.waterflow.enabled:
      if not (self.offline and isinstance(self.waterflow, MqttWaterflow)):
        try:
          self.waterflow.start()
        except Exception:
          self.logger.exception("Waterflow startup failed")
    if self.cfg.mqttEnabled and not self.offline:
      try:
        self.mqtt.start()
      except Exception:
        self.logger.exception("MQTT unavailable; local safety control remains active")
    self._status = "OK" if ready else "Terminating"
    if background:
      self.alerts.start()
      self.metrics.start()
      control = threading.Thread(target=self._control_loop, name="ValveControl", daemon=True)
      self.timer = threading.Thread(target=self._maintenance_loop, name="Scheduler", daemon=True)
      supervisor = threading.Thread(target=self._supervise, name="Supervisor", daemon=True)
      self.workers = [control]
      self._threads = [control, self.timer, supervisor]
      self._heartbeats = {"control": self.clock.monotonic(), "maintenance": self.clock.monotonic()}
      for thread in self._threads:
        thread.start()
    return ready

  def _control_loop(self):
    try:
      while not self._stop.is_set():
        self._drain_mqtt(stops_only=True)
        self.controller.tick()
        self._drain_mqtt()
        self.controller.tick()
        self._heartbeats["control"] = self.clock.monotonic()
        self._stop.wait(0.1)
    except Exception as error:
      self.logger.exception("Safety control thread failed")
      self._fail_safe("Safety control failed (%s)" % type(error).__name__)

  def _maintenance_loop(self):
    try:
      while not self._stop.is_set():
        self.maintenance_tick()
        self._heartbeats["maintenance"] = self.clock.monotonic()
        self._stop.wait(0.25)
    except Exception as error:
      self.logger.exception("Scheduler/monitoring thread failed")
      self._fail_safe("Scheduler failed (%s)" % type(error).__name__)

  def _supervise(self):
    while not self._stop.wait(0.25):
      if any(not thread.is_alive() for thread in self._threads[:2]):
        self._fail_safe("A critical control thread exited unexpectedly")
        return
      if any(self.clock.monotonic() - value > 10 for value in self._heartbeats.values()):
        self._fail_safe("A critical control thread stopped making progress")
        return

  def _close_bounded(self):
    self.controller.request_shutdown()
    with self._close_lock:
      if self._close_thread is None:
        def close():
          try:
            self._close_result = self.controller.shutdown()
          except Exception:
            self._close_result = False
            self.logger.exception("Safety close sequence failed")
        self._close_thread = threading.Thread(target=close, name="SafetyClose", daemon=True)
        self._close_thread.start()
      worker = self._close_thread
    worker.join(max(2.0, len(self.valves) * 0.7 + 0.5))
    if worker.is_alive():
      self.logger.critical("Safety close timed out; physical valve state is unknown")
      return False
    return self._close_result is True

  def _fail_safe(self, message):
    self._fatal_error = message
    self._stop.set()
    self.terminated = True
    self._status = "Terminating"
    closed = self._close_bounded()
    self.publishStatus()
    self.alerts.alert(
      AlertType.SAFETY_INTERVENTION, message,
      data={"close_commands_acknowledged": closed, "physical_state": "unverified"},
    )

  def exit_gracefully(self, signum=None, frame=None):
    self.controller.request_shutdown()
    self._stop.set()
    self.terminated = True

  def shutdown(self, reason="shutdown"):
    with self._shutdown_lock:
      if self._shutdown_done:
        return self._shutdown_result is True
      self._stop.set()
      self.terminated = True
      self._status = "Terminating"
      closed = self._close_bounded() if self._started else True
      if self._started:
        self.alerts.alert(
          AlertType.SYSTEM_EXIT, reason,
          data={"close_commands_acknowledged": closed, "physical_state": "unverified"},
        )
      if self.api_server is not None:
        self.api_server.should_exit = True
      for thread in self._threads:
        if thread is not threading.current_thread():
          thread.join(2)
          if thread.is_alive():
            self.logger.error("Thread '%s' did not stop within shutdown bound", thread.name)
      self.mqtt.shutdown()
      if self.waterflow:
        self.waterflow.shutdown()
      for sensor in self.sensors.values():
        sensor.shutdown()
      self.alerts.shutdown()
      self.metrics.shutdown()
      if self._temporary and not (self._close_thread and self._close_thread.is_alive()):
        self._temporary.cleanup()
      if self._async_log:
        self._async_log.shutdown()
      self._shutdown_result = closed
      self._shutdown_done = True
      return closed

  def _cancel_pending_opens(self, name, sequence=None):
    with self._mqtt_lock:
      barrier = self._mqtt_sequence if sequence is None else sequence
      self._mqtt_cancelled_through[name] = max(self._mqtt_cancelled_through.get(name, 0), barrier)

  def _mqtt_open_current(self, name, sequence):
    with self._mqtt_lock:
      return sequence > self._mqtt_cancelled_through.get(name, 0)

  def submit_mqtt(self, topic, payload):
    if self._stop.is_set():
      self.logger.error("MQTT command rejected during shutdown")
      return
    parts = topic.split("/")
    with self._mqtt_lock:
      self._mqtt_sequence += 1
      sequence = self._mqtt_sequence
      if len(parts) == 4 and parts[3] == "command" and parts[1] in ("forceopen", "forceclose"):
        name = parts[2].replace("_", " ")
        if name not in self.valves:
          self.logger.error("MQTT command rejected for unknown valve")
          return
        if parts[1] == "forceclose":
          self._mqtt_stops[name] = sequence
          self._mqtt_cancelled_through[name] = sequence
          return
        if self.controller.operations.get(name) is not None and name not in self._mqtt_stops:
          self.logger.error("MQTT Open rejected for owned valve '%s'; Close is required first", name)
          return
      try:
        self._mqtt_commands.put_nowait((sequence, topic, payload))
      except queue.Full:
        self.logger.error("MQTT command backlog full; command rejected without actuation")

  def _drain_mqtt(self, stops_only=False):
    with self._mqtt_lock:
      stops = list(self._mqtt_stops.items())
    for name, sequence in stops:
      topic = "%s/forceclose/%s/command" % (self.cfg.mqttClientName, name.replace(" ", "_"))
      self.mqtt.processMessages(topic, b"", command_sequence=sequence)
      with self._mqtt_lock:
        if self._mqtt_stops.get(name) == sequence:
          self._mqtt_stops.pop(name)
    if stops_only:
      return
    with self._mqtt_lock:
      try:
        sequence, topic, payload = self._mqtt_commands.get_nowait()
      except queue.Empty:
        return
      parts = topic.split("/")
      if len(parts) == 4 and parts[1] == "forceopen" and parts[3] == "command":
        name = parts[2].replace("_", " ")
        if sequence <= self._mqtt_cancelled_through.get(name, 0):
          self.logger.info("MQTT Open rejected for '%s': superseded by a later Close", name)
          self._mqtt_commands.task_done()
          return
        if name in self._mqtt_stops:
          self._mqtt_commands.put_nowait((sequence, topic, payload))
          self._mqtt_commands.task_done()
          return
    try:
      self.mqtt.processMessages(topic, payload, command_sequence=sequence)
    finally:
      self._mqtt_commands.task_done()

  def queueJob(self, job):
    self.controller.enqueue(job)
    self.logger.info("Queued '%s' for %s minutes", job.valve.name, job.duration)

  def update_config(self, mutator, *, enabled_updates=None):
    def publish():
      self.controller.apply_runtime_enabled_updates(enabled_updates or {})
    result = self.cfg.transaction(mutator, on_publish=publish)
    self.alerts.reload_config()
    self.controller.tick()
    return result

  def calculateScheduleTime(self, sched, now):
    return schedule_time(sched, now, self.cfg.timezone, *self.cfg.getLatLon())

  def shouldScheduleRun(self, sched, check_date=None, check_season=None):
    return should_run(sched, check_date or self.clock.now(), self.cfg.latitude, check_season)

  def evalSched(self, sched, timezone, now):
    return self.shouldScheduleRun(sched, now) and self.calculateScheduleTime(sched, now) == now

  def getSeason(self, lat, date=None):
    date = date if date is not None else self.clock.now()
    if not hasattr(date, "month"):
      date = SimpleNamespace(month=date)
    return season_for(lat, date)

  def calculateJobDuration(self, valve, sched):
    factor = None
    sensor = getattr(valve, "sensor", None)
    if getattr(sched, "enable_uv_adjustments", False) and sensor and sensor.enabled:
      try:
        factor = sensor.getFactor()
      except Exception as error:
        self.logger.warning("Using base duration for '%s'; weather adjustment unavailable (%s)",
                            valve.name, type(error).__name__)
    return adjusted_duration(sched, factor)

  def _schedule_tick(self):
    now = self.clock.now().astimezone(pytz.timezone(self.cfg.timezone)).replace(second=0, microsecond=0)
    for name, valve in self.valves.items():
      if not valve.enabled:
        continue
      for index, sched in enumerate(list(valve.schedules)):
        if self.evalSched(sched, self.cfg.timezone, now):
          key = (name, index, now.date(), now.hour, now.minute)
          if key in self._scheduled:
            continue
          self._scheduled[key] = True
          try:
            duration = self.calculateJobDuration(valve, sched)
            if duration > 0 and self.controller.ready:
              self.queueJob(Job(valve, duration, sched))
          except (ControlError, ValueError) as error:
            self.logger.error("Scheduled job %s for '%s' rejected: %s", index, name, error)
            self.alerts.alert(
              AlertType.SAFETY_INTERVENTION,
              "Scheduled job for '%s' rejected before actuation: %s" % (name, error),
              valve_name=name, data={"schedule_index": index, "error": type(error).__name__},
            )
    self._scheduled = {key: value for key, value in self._scheduled.items() if key[2] >= now.date()}

  def everyXMinutes(self, key, interval, bootstrap):
    now = self.clock.monotonic()
    if key not in self._intervalDict:
      self._intervalDict[key] = now
      return bootstrap
    if now - self._intervalDict[key] >= interval * 60:
      self._intervalDict[key] = now
      return True
    return False

  def _reload_baselines(self):
    detached = {name: SimpleNamespace(
      name=name, baseline_lpm=None, baseline_trend=None,
      baseline_std_dev=None, baseline_sample_count=0,
    ) for name in self.valves}
    self.metrics.load_baselines(detached)
    with self.controller.lock:
      for name, values in detached.items():
        for field in ("baseline_lpm", "baseline_trend", "baseline_std_dev", "baseline_sample_count"):
          setattr(self.valves[name], field, getattr(values, field))

  def maintenance_tick(self):
    self._monitor_health()
    date = self.clock.now().date()
    if date != self._baseline_date and self.everyXMinutes("baseline_refresh", 1, True):
      integrated_date = self.controller.prepare_daily_export()
      if integrated_date == date:
        self.metrics.flush()
        if not self.metrics.get_health().get("persistence_error"):
          self._reload_baselines()
          self._baseline_date = date
    if self.everyXMinutes("scheduler", 1, True):
      self._schedule_tick()
    events, finished = self.controller.drain_events()
    for snapshot in events:
      self.telemetryValve(snapshot)
    for operation in finished:
      if operation.complete:
        self.checkIrregularFlow(operation.valve, operation.open_seconds, operation.liters)
    if self.cfg.telemetry and self.everyXMinutes("idleInterval", self.cfg.telemIdleInterval, False):
      self.mqtt.publish("/svc/uptime", int((self.clock.monotonic() - self._start_mono) / 60))
      for valve in self.valves.values():
        self.telemetryValve(valve)
      self.publishStatus()
      for name, sensor in self.sensors.items():
        self.telemetrySensor(name, sensor)
    if self.cfg.telemetry and self.everyXMinutes("activeInterval", self.cfg.telemActiveInterval, False):
      for valve in self.valves.values():
        if valve.handled:
          self.telemetryValve(valve)
    if self.everyXMinutes("checkLeakInterval", 1, False):
      self._check_leak()

  def _monitor_waterflow(self):
    health = self.controller.get_waterflow_health()
    source = health.get("source", health)
    notification = self.waterflow.get_notification_state(startup_since=self._start_mono)
    subject = "resource:waterflow"
    if notification["reading_revision"] != self._waterflow_reading_revision:
      # A genuine observation can arrive and expire between monitoring ticks.
      self.alerts.clear_alert_state(AlertType.MONITORING_UNAVAILABLE, subject=subject)
      self._waterflow_reading_revision = notification["reading_revision"]
    if notification["enabled"]:
      reason = notification["reason"]
      if reason is not None:
        self.alerts.alert(
          AlertType.MONITORING_UNAVAILABLE, "Monitoring unavailable: waterflow (%s)" % reason,
          subject=subject, data={"source": "waterflow", "reason": reason},
        )
      elif source["available"]:
        self.alerts.clear_alert_state(AlertType.MONITORING_UNAVAILABLE, subject=subject)
    return health["enabled"] and not source["available"]

  def _monitor_health(self):
    lost = self._monitor_waterflow() if self.waterflow else False
    resources = [("sensor", sensor.name, sensor.get_health()) for sensor in self.sensors.values()]
    if self.cfg.mqttEnabled and not self.offline:
      resources.append(("resource", "mqtt", {"enabled": True, "available": self.mqtt.mqttStarted,
                                  "reason": "MQTT disconnected"}))
    if self._async_log:
      resources.append(("resource", "logging", self._async_log.get_health()))
    accounting = self.metrics.get_health()
    error = self.controller.accounting_error or accounting.get("persistence_error")
    if error:
      resources.append(("resource", "accounting", {"enabled": True, "available": False, "reason": error}))
    else:
      self.alerts.clear_alert_state(AlertType.MONITORING_UNAVAILABLE, subject="resource:accounting")
    for category, subject, health in resources:
      incident_subject = category + ":" + subject
      if health["enabled"] and not health["available"]:
        lost = True
        if category == "sensor":
          self.alerts.alert(
            AlertType.SENSOR_ERROR, "Sensor '%s' error: %s" % (subject, health.get("reason")),
            subject=subject, data={"sensor_name": subject, "error": health.get("reason")},
          )
        self.alerts.alert(
          AlertType.MONITORING_UNAVAILABLE, "Monitoring unavailable: %s (%s)" % (subject, health.get("reason")),
          subject=incident_subject, data={"source": subject, "reason": health.get("reason")},
        )
      else:
        self.alerts.clear_alert_state(AlertType.MONITORING_UNAVAILABLE, subject=incident_subject)
        if category == "sensor":
          self.alerts.clear_alert_state(AlertType.SENSOR_ERROR, subject=subject)
    with self._state_lock:
      if lost:
        self._tempStatus["SensorErr"] = True
      else:
        self._tempStatus.pop("SensorErr", None)
      if self.controller.ready and not self._stop.is_set():
        self._status = "OK"

  def allValvesClosed(self):
    with self.controller.lock:
      since = self.controller.closed_since
      return since is not None and self.clock.monotonic() - since >= 60

  def _check_leak(self):
    flow = self.waterflow
    if not flow or not flow.enabled or not flow.leakdetection or not self.allValvesClosed():
      return
    if self.alerts.is_in_exclusion_window(self.clock.now()):
      self.alerts.clear_alert_state(AlertType.LEAK)
      self.clearTempStatus("Leaking")
      return
    sample = flow.snapshot()
    if not sample["available"]:
      return
    rate = sample["value"]
    if rate > 0:
      self.alerts.alert(
        AlertType.LEAK, "Leak detected: %.2f L/min flow with all valves closed" % rate,
        data={"flow_rate_lpm": rate},
      )
      self.setTempStatus("Leaking")
    else:
      self.alerts.clear_alert_state(AlertType.LEAK)
      self.clearTempStatus("Leaking")

  def checkIrregularFlow(self, valve, total_seconds, total_liters):
    baseline, std = valve.baseline_lpm, valve.baseline_std_dev
    if baseline is None or std is None or total_seconds <= 0:
      return
    if not math.isfinite(baseline) or baseline <= 0 or not math.isfinite(std) or std < 0:
      self.logger.warning("Cannot compare flow against invalid/zero baseline for '%s'", valve.name)
      return
    threshold = self.cfg.cfg.alerts.irregular_flow_threshold
    overrides = getattr(self.cfg.cfg.alerts, "valve_overrides", None)
    if overrides and hasattr(overrides, valve.name):
      threshold = getattr(getattr(overrides, valve.name), "irregular_flow_threshold", threshold)
    actual = total_liters / total_seconds * 60
    if (baseline - std * threshold <= actual <= baseline + std * threshold
        or math.isclose(actual, baseline, rel_tol=1e-9, abs_tol=1e-9)):
      self.alerts.clear_alert_state(AlertType.IRREGULAR_FLOW, valve.name)
      return
    deviation = (actual - baseline) / baseline * 100
    direction = "above" if actual > baseline else "below"
    self.alerts.alert(
      AlertType.IRREGULAR_FLOW,
      "Valve '%s' flow rate %s baseline: %.2f L/min vs baseline %.2f L/min (%+.1f%%)" %
      (valve.name, direction, actual, baseline, deviation),
      valve_name=valve.name, data={
        "actual_lpm": round(actual, 2), "baseline_lpm": baseline,
        "baseline_std_dev": std, "threshold_std_devs": threshold,
        "deviation_percent": round(deviation, 2), "total_seconds": int(total_seconds + 1e-6),
        "total_liters": round(total_liters, 2),
      },
    )

  def setTempStatus(self, status):
    with self._state_lock:
      self._tempStatus[status] = True
    self.publishStatus()

  def clearTempStatus(self, status):
    with self._state_lock:
      self._tempStatus.pop(status, None)
    self.publishStatus()

  def setStatus(self, status):
    self._status = status
    self.publishStatus()

  def publishStatus(self):
    with self._state_lock:
      status = ",".join(self._tempStatus) if self._tempStatus else self._status
    self.mqtt.publish("/svc/status", status)

  def telemetryValve(self, valve):
    if isinstance(valve, dict):
      values = valve
    else:
      values = self.controller.valve_snapshot(valve.name)
    name = values["name"]
    status = "open" if values["is_open"] else "enabled" if values["enabled"] else "disabled"
    operation = self.controller.operations.get(name)
    if self.controller.faults.get(name) or (values["is_open"] and operation and operation.no_flow):
      status = "malfunction"
    self.mqtt.publish(name + "/secondsLast", values["seconds_last"])
    if self.waterflow and self.waterflow.started:
      self.mqtt.publish(name + "/litersLast", values["liters_last"])
    self.mqtt.publish(name + "/status", status)
    self.mqtt.publish(name + "/dailytotal", values["seconds_daily"])
    if self.waterflow and self.waterflow.started:
      self.mqtt.publish(name + "/dailyliters", values["liters_daily"])
    self.mqtt.publish(name + "/remaining", values["seconds_remain"])

  def reset_telemetry_cursor(self):
    with self._state_lock:
      self._sensor_cursors = {}
      self._mqtt_generation += 1

  def telemetrySensor(self, name, sensor):
    prefix = "sensor/" + name + "/"
    with self._state_lock:
      generation = self._mqtt_generation
    try:
      factor = sensor.getFactor()
      disabled = sensor.shouldDisable()
      status = "Disabled" if disabled else "Factored" if factor != 1 else "Enabled"
      self.mqtt.publish(prefix + "factor", factor)
      revision = getattr(sensor, "revision", None)
      if revision is None or self._sensor_cursors.get(name) != revision:
        telemetry = sensor.getTelemetry()
        sent = [self.mqtt.publish(prefix + key, value) for key, value in telemetry.items()]
        if all(sent):
          with self._state_lock:
            if generation == self._mqtt_generation:
              self._sensor_cursors[name] = revision
    except Exception:
      status = "Error"
    self.mqtt.publish(prefix + "status", status)

  def get_health(self):
    health = self.controller.get_health()
    if self._fatal_error:
      health["controller"]["last_error"] = self._fatal_error
      health["controller"]["fault"] = True
    health["controller"]["heartbeat_age_seconds"] = {
      name: max(0.0, self.clock.monotonic() - stamp) for name, stamp in self._heartbeats.items()
    }
    if self._started and self._threads:
      running = all(thread.is_alive() for thread in self._threads[:2])
      health["controller"]["running"] = running
      health["ready"] = health["ready"] and running
    health["monitoring"] = {
      "mqtt": {"enabled": self.cfg.mqttEnabled, "connected": self.mqtt.mqttStarted},
      "waterflow": self.controller.get_waterflow_health() if self.waterflow else {
        "enabled": False, "available": False, "fresh": False, "age_seconds": None, "reason": "not configured",
      },
      "sensors": [sensor.get_health() for sensor in self.sensors.values()],
    }
    health["alerts"] = self.alerts.get_health()
    health["accounting"] = self.metrics.get_health()
    if self.controller.accounting_error:
      health["accounting"]["persistence_error"] = self.controller.accounting_error
    health["offline_mode"] = self.offline
    if self._async_log:
      health["logging"] = self._async_log.get_health()
    return health


def main(argv=None):
  parser = argparse.ArgumentParser(description="Irrigation controller")
  parser.add_argument("--config", default="config.json")
  parser.add_argument("--test", action="store_true", help="Offline initialization check; no GPIO/network/server")
  parser.add_argument("--simulate", nargs="?", const="", default=None)
  args = parser.parse_args((argv or sys.argv)[1:])
  offline = args.test or args.simulate is not None
  app = Irrigate(args.config, offline=offline)
  if args.simulate is not None:
    from schedule_simulator import ScheduleSimulator
    simulator = ScheduleSimulator(app)
    simulator.parse_schedule_options(args.simulate)
    simulator.print_schedule()
    app.shutdown()
    return 0
  if args.test:
    app.logger.info("Offline initialization check: no GPIO, network, or API server")
    try:
      return 0 if app.start(background=False) else 1
    finally:
      app.shutdown("offline test complete")
  for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, app.exit_gracefully)
  from api_server import run_api_server
  try:
    app.start(test=False)
    if app._stop.is_set():
      return 0
    api_thread = threading.Thread(target=run_api_server, args=(app,), name="API", daemon=True)
    api_thread.start()
    while not app._stop.wait(0.25):
      if not api_thread.is_alive():
        app._fail_safe("API server exited unexpectedly")
    return 1 if app._fatal_error else 0
  finally:
    app.shutdown("system shutdown")


if __name__ == "__main__":
  sys.exit(main())
