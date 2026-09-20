import math
import queue
import threading
from collections import deque
from datetime import timedelta

from alerts import AlertType
from model import Job, Operation
from scheduling import duration_seconds


class ControlError(RuntimeError):
  def __init__(self, message, status_code=400):
    super().__init__(message)
    self.status_code = status_code


def duration_minutes(value, manual=False):
  if value is None and manual:
    return 30.0
  if isinstance(value, bool) or value is None:
    raise ControlError("Duration must be a finite positive number")
  try:
    minutes = float(value)
  except (ValueError, TypeError, OverflowError) as error:
    raise ControlError("Duration must be a bare finite positive number") from error
  if not math.isfinite(minutes) or minutes <= 0:
    raise ControlError("Duration must be a finite positive number")
  if manual:
    minutes = min(minutes, 30.0)
  try:
    duration_seconds(minutes)
  except ValueError as error:
    raise ControlError("Duration cannot be represented safely: %s" % error) from error
  return minutes


class ValveController:
  def __init__(self, valves, concurrency, clock, logger, alerts, waterflow=None, metrics=None,
               on_stop=None, is_open_current=None):
    self.valves = valves
    self.concurrency = concurrency
    self.clock = clock
    self.logger = logger
    self.alerts = alerts
    self.waterflow = waterflow
    self.metrics = metrics
    self.on_stop = on_stop
    self.is_open_current = is_open_current
    self.lock = threading.RLock()
    self.q = queue.Queue()
    self.operations = {}
    self.states = {name: "unknown" for name in valves}
    self.faults = {name: None for name in valves}
    self.quality = {name: (True, None) for name in valves}
    self._runtime_enabled = {}
    self._identifier = 0
    self._startup_complete = False
    self._stopping = False
    self._last_mono = clock.monotonic()
    self._last_wall = clock.now()
    self._date = self._last_wall.date()
    self._events = deque()
    self._finished = deque()
    self.last_error = None
    self.accounting_error = None
    self.closed_since = None
    for valve in self.valves.values():
      valve.is_open = True

  @property
  def ready(self):
    return self._startup_complete and not self._stopping and not any(self.faults.values())

  def _valve(self, name):
    if name not in self.valves:
      raise ControlError("Valve '%s' not found" % name, 404)
    return self.valves[name]

  def _duration(self, value, manual=False):
    minutes = duration_minutes(value, manual)
    seconds = minutes * 60
    now = self.clock.monotonic()
    if not math.isfinite(now + seconds) or now + seconds <= now:
      raise ControlError("Duration cannot be represented by the monotonic clock")
    return minutes, seconds

  def _admit(self):
    if not self.ready:
      raise ControlError("Watering unavailable: startup reconciliation, shutdown, or actuator fault", 503)

  def reconcile_startup(self):
    with self.lock:
      for valve in self.valves.values():
        self._close_driver(valve, clear_fault=True)
      self._startup_complete = True
      self._last_mono = self.clock.monotonic()
      self._last_wall = self.clock.now()
      return self.ready

  def _emit(self, valve):
    self._events.append(self.valve_snapshot(valve.name))
    if len(self._events) > 1024:
      self._events.popleft()
      self.logger.error("Telemetry transition backlog overflow; latest state remains available")

  def _failure(self, valve, action, error):
    message = "%s command failed for '%s': %s" % (action, valve.name, type(error).__name__)
    self.last_error = message
    self.faults[valve.name] = message
    self.states[valve.name] = "fault"
    valve.is_open = True
    self.quality[valve.name] = (False, "actuation state uncertain")
    self.closed_since = None
    self.logger.error(message, exc_info=True)
    self.alerts.alert(
      AlertType.ACTUATION_FAILURE, message + "; physical state is unverified",
      valve_name=valve.name, data={"action": action, "physical_state": "unknown"},
    )

  def _close_driver(self, valve, clear_fault=False):
    self._safe_integrate()
    for attempt in range(3):
      try:
        valve.close()
        self._safe_integrate()
        valve.is_open = False
        self.states[valve.name] = "closed"
        if clear_fault:
          self.faults[valve.name] = None
          self.alerts.clear_alert_state(AlertType.ACTUATION_FAILURE, valve.name)
        if not any(self.faults.values()) and all(not item.is_open for item in self.valves.values()):
          if self.closed_since is None:
            self.closed_since = self.clock.monotonic()
        self._emit(valve)
        self.logger.info("Close command acknowledged for '%s' (physical state unverified)", valve.name)
        return True
      except Exception as error:
        self.logger.error("Close attempt %s/3 failed for '%s' (%s)",
                          attempt + 1, valve.name, type(error).__name__)
        if attempt == 2:
          self._failure(valve, "close", error)
    return False

  def _open_driver(self, operation):
    valve = operation.valve
    self._safe_integrate()
    if (operation.cancelled or self.operations.get(valve.name) is not operation or not self.ready
        or (operation.kind != "manual" and
            (not valve.enabled or self.clock.monotonic() >= operation.deadline))):
      return False
    self.closed_since = None
    try:
      valve.open()
      self._safe_integrate()
      valve.is_open = True
      operation.paused = False
      operation.last_positive = self.clock.monotonic()
      self.states[valve.name] = "open"
      self.logger.info("Open command acknowledged for '%s' (%s, operation %s)",
                       valve.name, operation.kind, operation.identifier)
      self._emit(valve)
      return True
    except Exception as error:
      operation.cancelled = True
      operation.complete = False
      operation.quality_reason = "open actuation failed"
      self._failure(valve, "open", error)
      closed = self._close_driver(valve)
      self.states[valve.name] = "fault"
      valve.is_open = True
      self.alerts.alert(
        AlertType.SAFETY_INTERVENTION,
        "Opening failed; close command %s for '%s' (physical state unverified)" %
        ("acknowledged" if closed else "also failed", valve.name),
        valve_name=valve.name, data={"close_command_acknowledged": closed},
      )
      return False

  def _new_operation(self, job, kind, seconds):
    self._identifier += 1
    now = self.clock.monotonic()
    operation = Operation(self._identifier, job, kind, seconds, now + seconds, now)
    self.operations[job.valve.name] = operation
    valve = job.valve
    valve.handled = True
    valve.secondsLast = 0
    valve.litersLast = 0.0
    valve.secondsDuration = int(math.ceil(seconds))
    valve.secondsRemain = int(math.ceil(seconds))
    valve.waterflow = self.waterflow
    self.states[valve.name] = "waiting"
    self.logger.info("Operation %s claimed '%s' for %.6g seconds (%s)",
                     operation.identifier, valve.name, seconds, kind)
    self.alerts.clear_alert_state(AlertType.SAFETY_INTERVENTION, valve.name)
    return operation

  def start_manual(self, name, duration=None, *, command_sequence=None):
    with self.lock:
      valve = self._valve(name)
      if (command_sequence is not None and self.is_open_current
          and not self.is_open_current(name, command_sequence)):
        raise ControlError("Open rejected: superseded by a later Close", 409)
      try:
        minutes, seconds = self._duration(duration, manual=True)
      except ControlError:
        self.logger.error("Rejected invalid manual duration for '%s'", name)
        raise
      self._admit()
      if name in self.operations:
        self.logger.warning("Open conflict for owned valve '%s'; Close is required first", name)
        raise ControlError("Valve '%s' already has an operation; Close before changing it" % name, 409)
      self._safe_integrate()
      operation = self._new_operation(Job(valve, minutes, None), "manual", seconds)
      if not self._open_driver(operation):
        raise ControlError("Open command failed; closure is unverified. See health and retry Close", 503)
      operation.deadline = self.clock.monotonic() + seconds
      return operation

  def enqueue(self, job):
    with self.lock:
      self._admit()
      self._valve(job.valve.name)
      try:
        minutes, _ = self._duration(job.duration)
      except ControlError:
        self.logger.error("Rejected invalid queue duration for '%s'", job.valve.name)
        raise
      if not isinstance(job.duration, (int, float)):
        job.duration = minutes
      self.q.put(job)

  def queue_snapshot(self):
    with self.q.mutex:
      return list(self.q.queue)

  def _complete(self, operation, reason):
    valve = operation.valve
    operation.cancelled = True
    valve.secondsRemain = 0
    if not self._close_driver(valve, clear_fault=reason == "stop"):
      operation.complete = False
      operation.quality_reason = "close actuation failed"
      return False
    self.operations.pop(valve.name, None)
    valve.handled = False
    self.quality[valve.name] = (operation.complete, operation.quality_reason)
    if operation.kind != "manual":
      self.q.task_done()
    self._finished.append(operation)
    self.logger.info("Operation %s ended for '%s': %s, %.3fs, %.3f estimated liters",
                     operation.identifier, valve.name, reason, operation.open_seconds, operation.liters)
    if not operation.no_flow:
      self.alerts.clear_alert_state(AlertType.MALFUNCTION_NO_FLOW, valve.name)
    self._emit(valve)
    if self.metrics:
      self.metrics.request_flush()
    return True

  def stop(self, name, *, command_sequence=None):
    if name not in self.states:
      raise ControlError("Valve '%s' not found" % name, 404)
    if self.on_stop:
      self.on_stop(name, command_sequence)
    with self.lock:
      valve = self._valve(name)
      self._safe_integrate()
      operation = self.operations.get(name)
      success = (self._complete(operation, "stop") if operation else
                 self._close_driver(valve, clear_fault=True))
      valve.secondsRemain = 0
      if not success:
        raise ControlError("Close command failed; valve may still be open. See health and retry Close", 503)
      if not any(self.faults.values()):
        self.last_error = None

  def set_enabled(self, name, enabled):
    with self.lock:
      valve = self._valve(name)
      if not isinstance(enabled, bool):
        self.logger.error("Rejected invalid enabled value for '%s'", name)
        raise ControlError("Enabled must be a boolean")
      self._safe_integrate()
      self._runtime_enabled[name] = enabled
      valve.enabled = enabled
      operation = self.operations.get(name)
      closed = False
      if operation and operation.kind != "manual" and not enabled:
        closed = self._complete(operation, "disabled")
      if not closed:
        self._emit(valve)

  def apply_runtime_enabled_updates(self, enabled_updates):
    with self.lock:
      for name, enabled in self._runtime_enabled.items():
        self.valves[name].enabled = enabled
      for name, enabled in enabled_updates.items():
        self._runtime_enabled.pop(name, None)
        self.valves[name].enabled = enabled

  def _sensor_disabled(self, operation):
    sensor = operation.job.sensor
    if sensor is None or not sensor.enabled:
      return False
    try:
      disabled = sensor.shouldDisable()
      self.alerts.clear_alert_state(AlertType.SENSOR_ERROR, subject=sensor.name)
      return disabled
    except Exception as error:
      self.alerts.alert(
        AlertType.SENSOR_ERROR,
        "Sensor '%s' error: %s" % (sensor.name, error),
        subject=sensor.name, data={"sensor_name": sensor.name, "error": str(error)},
      )
      self.alerts.alert(
        AlertType.MONITORING_UNAVAILABLE,
        "Weather '%s' unavailable; continuing within the existing deadline" % sensor.name,
        subject="sensor:" + sensor.name, data={"sensor_name": sensor.name, "error": type(error).__name__},
      )
      return False

  def _integrate(self):
    now = self.clock.monotonic()
    observed_wall = self.clock.now()
    if not math.isfinite(now) or now < self._last_mono:
      raise RuntimeError("Invalid monotonic clock")
    if now == self._last_mono:
      return
    # Do not export a day that a later wall-clock read, but not integration, has passed.
    integrated_date = min(
      (self._last_wall + timedelta(seconds=now - self._last_mono)).date(),
      observed_wall.date(),
    )
    open_ops = [operation for operation in self.operations.values()
                if self.states[operation.valve.name] == "open"]
    uncertain = any(state in ("unknown", "fault") for state in self.states.values())
    intervals = (self.waterflow.intervals(self._last_mono, now) if self.waterflow else
                 [(self._last_mono, now, None)])
    for left, right, rate in intervals:
      seconds = right - left
      liters = 0.0 if rate is None else rate / 60 * seconds
      if not math.isfinite(liters) or any(not math.isfinite(op.liters + liters) for op in open_ops):
        self.logger.error("Flow accounting overflow; interval is unavailable, not zero flow")
        self.alerts.alert(AlertType.MONITORING_UNAVAILABLE, "Flow accounting overflow",
                          subject="resource:waterflow", data={"source": "waterflow"})
        rate, liters = None, 0.0
      attributable = rate is not None and len(open_ops) == 1 and not uncertain
      contributions = {}
      for operation in open_ops:
        operation.open_seconds += seconds
        if attributable:
          operation.liters += liters
          operation.attributable_seconds += seconds
          if operation.last_positive is None or rate > 0:
            operation.last_positive = right if rate > 0 else left
          if rate > 0:
            operation.no_flow = False
            self.alerts.clear_alert_state(AlertType.MALFUNCTION_NO_FLOW, operation.valve.name)
        else:
          operation.complete = False
          operation.quality_reason = ("flow monitoring unavailable" if rate is None else
                                      "shared flow cannot be attributed")
          operation.last_positive = None
        contributions[operation.valve.name] = {
          "seconds": seconds, "liters": liters if attributable else 0.0,
          "complete": attributable,
        }
        operation.valve.secondsLast = int(operation.open_seconds + 1e-6)
        operation.valve.litersLast = operation.liters
      if self.metrics:
        wall_start = self._last_wall + timedelta(seconds=left - self._last_mono)
        self.metrics.add_interval(
          wall_start, seconds, contributions,
          unattributed_liters=0.0 if attributable else liters,
          unavailable_seconds=seconds if rate is None and open_ops else 0.0,
        )
      else:
        for operation in open_ops:
          operation.valve.secondsDaily += seconds
          operation.valve.litersDaily += liters if attributable else 0.0
    self._last_mono = now
    self._last_wall = observed_wall
    if self.metrics:
      if self._date != integrated_date:
        self.metrics.finalize_before(integrated_date)
        self.metrics.request_flush()
        self._date = integrated_date
      self.restore_daily()

  def _safe_integrate(self):
    try:
      self._integrate()
      if self.accounting_error:
        self.accounting_error = None
        self.alerts.clear_alert_state(AlertType.MONITORING_UNAVAILABLE, subject="resource:accounting")
    except Exception as error:
      message = "Accounting unavailable (%s); valve safety remains active" % type(error).__name__
      if self.accounting_error != message:
        self.logger.exception(message)
      self.accounting_error = message
      self._last_mono = self.clock.monotonic()
      self._last_wall = self.clock.now()
      for operation in self.operations.values():
        operation.complete = False
        operation.quality_reason = "accounting incomplete"
      self.alerts.alert(AlertType.MONITORING_UNAVAILABLE, message, subject="resource:accounting")

  def restore_daily(self):
    if not self.metrics:
      return
    totals = self.metrics.daily_totals(self.clock.now().date())
    for name, valve in self.valves.items():
      values = totals.get(name, {})
      valve.secondsDaily = int(values.get("seconds", 0.0) + 1e-6)
      valve.litersDaily = values.get("liters", 0.0)

  def prepare_daily_export(self):
    with self.lock:
      self._safe_integrate()
      if self.accounting_error:
        return None
      if self.metrics:
        self.metrics.finalize_before(self._date)
        self.metrics.request_flush()
      return self._date

  def tick(self):
    with self.lock:
      self._safe_integrate()
      for name, operation in list(self.operations.items()):
        now = self.clock.monotonic()
        valve = operation.valve
        if operation.cancelled:
          continue
        if (self.states[name] == "open" and operation.last_positive is not None
            and now - operation.last_positive >= 60):
          operation.no_flow = True
          self.alerts.alert(
            AlertType.MALFUNCTION_NO_FLOW,
            "Valve '%s' has no observed positive flow for at least 60s" % name,
            valve_name=name,
            data={"seconds_open": valve.secondsLast, "liters_detected": valve.litersLast},
          )
        if self._stopping or now >= operation.deadline:
          late = now - operation.deadline
          closed = self._complete(operation, "shutdown" if self._stopping else "complete")
          if not self._stopping and late > 1:
            self.alerts.alert(
              AlertType.SAFETY_INTERVENTION,
              "Deadline for '%s' was serviced %.2fs late; close command %s" %
              (name, late, "acknowledged" if closed else "failed"),
              valve_name=name, data={"late_seconds": late, "close_command_acknowledged": closed},
            )
          continue
        if operation.kind != "manual" and not valve.enabled:
          self._complete(operation, "disabled")
          continue
        disabled = operation.kind != "manual" and self._sensor_disabled(operation)
        if operation.cancelled or self.operations.get(name) is not operation:
          continue
        if disabled and self.states[name] == "open":
          if self._close_driver(valve):
            operation.paused = True
            self.states[name] = "paused"
          else:
            operation.cancelled = True
        elif not disabled and self.states[name] in ("waiting", "paused"):
          self._open_driver(operation)
        elif disabled:
          self.states[name] = "paused"
          operation.paused = True
        valve.secondsRemain = max(0, int(math.ceil(operation.deadline - self.clock.monotonic())))
      while self.ready and sum(op.kind != "manual" for op in self.operations.values()) < self.concurrency:
        if any(not op.cancelled and self.clock.monotonic() >= op.deadline for op in self.operations.values()):
          break
        with self.q.mutex:
          job = self.q.queue[0] if self.q.queue else None
        if job is None or job.valve.name in self.operations:
          break
        job = self.q.get_nowait()
        if not job.valve.enabled:
          self.logger.info("Discarding disabled queued valve '%s' before actuation", job.valve.name)
          self.q.task_done()
          continue
        try:
          _, seconds = self._duration(job.duration)
        except ControlError as error:
          self.q.task_done()
          self.logger.error("Queued job for '%s' rejected before actuation: %s", job.valve.name, error)
          self.alerts.alert(
            AlertType.SAFETY_INTERVENTION,
            "Queued job for '%s' rejected before actuation: %s" % (job.valve.name, error),
            valve_name=job.valve.name, data={"error": type(error).__name__},
          )
          continue
        operation = self._new_operation(job, "scheduled" if job.sched else "queued", seconds)
        disabled = self._sensor_disabled(operation)
        if operation.cancelled or self.operations.get(job.valve.name) is not operation:
          continue
        if disabled:
          operation.paused = True
          self.states[job.valve.name] = "paused"
        else:
          self._open_driver(operation)
      return self.ready

  def shutdown(self):
    with self.lock:
      self._stopping = True
      self._safe_integrate()
      all_acknowledged = True
      for valve in self.valves.values():
        operation = self.operations.get(valve.name)
        if operation:
          acknowledged = self._complete(operation, "shutdown")
        else:
          acknowledged = self._close_driver(valve)
        all_acknowledged = all_acknowledged and acknowledged
      while True:
        try:
          self.q.get_nowait()
          self.q.task_done()
        except queue.Empty:
          break
      if self.metrics:
        self.metrics.request_flush()
      return all_acknowledged

  def request_shutdown(self):
    self._stopping = True

  def valve_snapshot(self, name):
    with self.lock:
      valve = self.valves[name]
      return {
        "name": name, "enabled": valve.enabled, "is_open": valve.is_open,
        "handled": valve.handled, "seconds_daily": int(valve.secondsDaily),
        "liters_daily": valve.litersDaily, "seconds_remain": valve.secondsRemain,
        "seconds_duration": valve.secondsDuration, "seconds_last": valve.secondsLast,
        "liters_last": valve.litersLast,
      }

  def snapshot(self):
    with self.lock:
      return [self.valve_snapshot(name) for name in self.valves]

  def get_health(self):
    with self.lock:
      totals = self.metrics.daily_totals(self.clock.now().date()) if self.metrics else {}
      valves = []
      for name, valve in self.valves.items():
        operation = self.operations.get(name)
        complete, reason = ((operation.complete, operation.quality_reason) if operation else self.quality[name])
        if not totals.get(name, {}).get("complete", True):
          complete, reason = False, reason or "daily total is partial"
        if self.accounting_error or (self.metrics and self.metrics.get_health().get("persistence_error")):
          complete, reason = False, "accounting unavailable; totals may be incomplete"
        valves.append({
          "name": name, "state": self.states[name],
          "operation": operation.kind if operation else None,
          "fault": self.faults[name],
          "possibly_open": valve.is_open or self.states[name] in ("unknown", "fault"),
          "physical_state": "unverified",
          "flow_alarm": bool(operation and operation.no_flow),
          "attribution": {"complete": complete, "reason": reason,
                          "method": "single-commanded-valve shared-meter estimate"},
        })
      return {
        "ready": self.ready,
        "controller": {"running": not self._stopping, "fault": any(self.faults.values()),
                       "last_error": self.last_error},
        "valves": valves,
      }

  def drain_events(self):
    with self.lock:
      events, finished = list(self._events), list(self._finished)
      self._events.clear()
      self._finished.clear()
      return events, finished
