from copy import deepcopy
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
import math
import threading
import time
from typing import Optional, Dict, Any, List, Tuple

from alert_channels import channelFactory


class AlertType(Enum):
    LEAK = "leak"
    MALFUNCTION_NO_FLOW = "malfunction_no_flow"
    IRREGULAR_FLOW = "irregular_flow"
    SYSTEM_EXIT = "system_exit"
    SENSOR_ERROR = "sensor_error"
    SAFETY_INTERVENTION = "safety_intervention"
    ACTUATION_FAILURE = "actuation_failure"
    MONITORING_UNAVAILABLE = "monitoring_unavailable"


class AlertSeverity(Enum):
    WARNING = "warning"
    CRITICAL = "critical"


# Map alert types to their severity
ALERT_SEVERITY_MAP = {
    AlertType.LEAK: AlertSeverity.CRITICAL,
    AlertType.MALFUNCTION_NO_FLOW: AlertSeverity.WARNING,
    AlertType.IRREGULAR_FLOW: AlertSeverity.WARNING,
    AlertType.SYSTEM_EXIT: AlertSeverity.CRITICAL,
    AlertType.SENSOR_ERROR: AlertSeverity.WARNING,
    AlertType.SAFETY_INTERVENTION: AlertSeverity.CRITICAL,
    AlertType.ACTUATION_FAILURE: AlertSeverity.CRITICAL,
    AlertType.MONITORING_UNAVAILABLE: AlertSeverity.WARNING,
}


@dataclass
class Alert:
    """Represents a single alert occurrence"""
    type: AlertType
    valve_name: Optional[str]  # None for system-wide alerts
    timestamp: datetime
    message: str
    data: Dict[str, Any]  # Context data (flow rates, baselines, etc.)
    
    @property
    def severity(self) -> AlertSeverity:
        return ALERT_SEVERITY_MAP[self.type]
    
    def to_dict(self):
        """Convert alert to dictionary for logging/serialization"""
        return {
            "type": self.type.value,
            "severity": self.severity.value,
            "valve_name": self.valve_name,
            "timestamp": self.timestamp.isoformat(),
            "message": self.message,
            "data": self.data
        }


class _SystemClock:
    @staticmethod
    def monotonic():
        return time.monotonic()

    @staticmethod
    def now():
        return datetime.now()


@dataclass
class _Incident:
    last_occurrence: float
    delivery_failed: bool = False


@dataclass
class _ChannelDelivery:
    channel: Any
    due: float
    configuration: Any
    attempts: int = 0
    status: str = "pending"


@dataclass
class _Delivery:
    key: Tuple[AlertType, Optional[str], Optional[str]]
    incident: _Incident
    alert: Alert
    channels: List[_ChannelDelivery]


class AlertManager:
    """In-memory, bounded alert outbox with explicit worker ownership.

    Construction and alert() never send. Call start() after safety initialization;
    close hardware before shutdown(). Recovery, not successful delivery, ends an
    incident. Subject identifies an internal resource and never enters the payload.
    Channel constructors must be local-only and send() must make one bounded attempt.
    """

    DEFAULT_QUEUE_CAPACITY = 128
    MIN_INCIDENT_CAPACITY = 1024
    MAX_ATTEMPTS = 5

    def __init__(self, logger, config, irrigate_instance, *, clock=None,
                 channel_factory=None, queue_capacity=DEFAULT_QUEUE_CAPACITY):
        if isinstance(queue_capacity, bool) or not isinstance(queue_capacity, int) or queue_capacity < 1:
            raise ValueError("queue_capacity must be a positive integer")
        self.logger = logger
        self.config = config
        self.irrigate = irrigate_instance
        self._clock = clock if clock is not None else _SystemClock()
        self._channel_factory = channel_factory if channel_factory is not None else channelFactory
        self._capacity = queue_capacity
        # Never evict an unrecovered incident: doing so would notify it twice.
        self._incident_capacity = max(self.MIN_INCIDENT_CAPACITY, queue_capacity)
        self._condition = threading.Condition()
        self._dispatch_lock = threading.Lock()
        self._reload_lock = threading.Lock()
        self._incidents: Dict[tuple, _Incident] = {}
        self._outbox: Dict[tuple, _Delivery] = {}
        self._inflight = None
        self._worker = None
        self._status = "not_started"
        self._accepting = True
        self._stop_requested = False
        self._shutdown_deadline = None
        self._shutdown_timed_out = False
        self._worker_error = None
        self._last_error = None
        self._configuration_errors = 0
        self._overflowed = False
        self._errors = {
            "overflow": 0, "incident_overflow": 0, "failed_attempts": 0,
            "retry_exhausted": 0, "channel_exceptions": 0, "worker_exceptions": 0,
            "channel_initialization": 0, "exclusion_errors": 0,
            "shutdown_timeouts": 0, "rejected_after_shutdown": 0,
        }
        self._counts = {
            "occurrences": 0, "coalesced": 0, "delivered": 0,
            "delivered_channels": 0, "retries_scheduled": 0, "cancelled": 0,
        }
        self.enabled = {}
        self.leak_repeat_minutes = 15
        self.leak_detection_exclusions = ()
        self.channels: List = []
        self._channel_specs = []
        self.reload_config()
        self.logger.info(f"AlertManager initialized with {len(self.channels)} channel(s)")

    def _error_locked(self, code, exception=None):
        self._errors[code] += 1
        # Exception messages can contain request URLs, credentials and payloads.
        self._last_error = code if exception is None else f"{code} ({type(exception).__name__})"

    def reload_config(self):
        """Reload local settings without starting a worker or sending.

        Disabling a type cancels its incident; re-enabling permits a fresh one.
        Unchanged channels retain per-channel successes and retry budgets. Changed
        channels replace only their pending attempts, never retry an old endpoint.
        Unavailable channels retain queued work until an explicit reload repairs
        them. An already executing attempt cannot be interrupted.
        """
        with self._reload_lock:
            cfg = self.config.cfg.alerts
            enabled = {kind: getattr(cfg.enabled, kind.value, True) for kind in AlertType}
            repeat = float(getattr(cfg, "leak_repeat_minutes", 15))
            if not math.isfinite(repeat) or repeat < 0:
                raise ValueError("leak_repeat_minutes must be finite and nonnegative")
            exclusions = tuple(deepcopy(getattr(cfg, "leak_detection_exclusions", [])))
            channel_configs = deepcopy(getattr(cfg, "channels", []))
            with self._condition:
                reusable = list(self._channel_specs)
            specs = []
            failures = []
            for channel_cfg in channel_configs:
                match = next((i for i, (old_cfg, old_channel) in enumerate(reusable)
                              if old_cfg == channel_cfg and old_channel is not None), None)
                if match is not None:
                    specs.append(reusable.pop(match))
                    continue
                try:
                    channel = self._channel_factory(self.logger, channel_cfg)
                    if not callable(getattr(channel, "send", None)):
                        raise TypeError("Alert channel must provide send()")
                    specs.append((channel_cfg, channel))
                except Exception as exc:
                    failures.append(exc)
                    specs.append((channel_cfg, None))

            now = self._clock.monotonic()
            with self._condition:
                self.enabled = enabled
                self.leak_repeat_minutes = repeat
                self.leak_detection_exclusions = exclusions
                self._channel_specs = specs
                self.channels = [channel for _, channel in specs if channel is not None]
                self._configuration_errors = len(failures)
                for exc in failures:
                    self._error_locked("channel_initialization", exc)
                for key in list(self._incidents):
                    if not enabled[key[0]]:
                        self._clear_locked(key)
                for delivery in list(self._outbox.values()):
                    reusable_attempts = list(delivery.channels)
                    attempts = []
                    for channel_cfg, channel in specs:
                        match = next((i for i, old in enumerate(reusable_attempts)
                                      if old.configuration == channel_cfg
                                      and (old.channel is channel or old.channel is None)), None)
                        if match is None:
                            attempt = _ChannelDelivery(
                                channel, now, channel_cfg,
                                status="pending" if channel is not None else "unavailable",
                            )
                        else:
                            attempt = reusable_attempts.pop(match)
                            attempt.channel = channel
                            if channel is not None and attempt.status == "unavailable":
                                attempt.status = "pending"
                                attempt.due = now
                        attempts.append(attempt)
                    delivery.channels = attempts
                    if not attempts:
                        self._counts["cancelled"] += 1
                    self._finish_locked(delivery)
                self._condition.notify_all()
            for exc in failures:
                self.logger.error(f"Failed to initialize alert channel ({type(exc).__name__})")
        return not failures

    def start(self):
        """Start one daemon delivery worker; an explicit restart preserves retries."""
        with self._condition:
            if self._worker is not None and self._worker.is_alive():
                return self._status == "running"
            self._stop_requested = False
            self._shutdown_deadline = None
            self._shutdown_timed_out = False
            self._accepting = True
            self._worker_error = None
            self._status = "running"
            try:
                self._worker = threading.Thread(
                    target=self._run, name="AlertDelivery", daemon=True,
                )
                self._worker.start()
            except Exception as exc:
                self._status = "failed"
                self._accepting = False
                self._stop_requested = True
                self._worker_error = type(exc).__name__
                self._error_locked("worker_exceptions", exc)
                self._worker = None
                error = type(exc).__name__
            else:
                return True
        self.logger.error(f"Alert delivery worker failed to start ({error})")
        return False

    def shutdown(self, timeout=2.0):
        """Drain until a real-time deadline, then stop; return whether joined.

        A blocked channel is never joined past timeout. Its daemon worker finishes
        when send() returns and cannot be restarted while still alive. Undelivered
        work stays bounded in memory for an explicit start(); nothing is persisted.
        """
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and nonnegative")
        deadline = time.monotonic() + timeout
        with self._condition:
            self._accepting = False
            if self._shutdown_deadline is None:
                self._shutdown_deadline = deadline
            else:
                self._shutdown_deadline = min(deadline, self._shutdown_deadline)
            worker = self._worker
            if worker is None or not worker.is_alive():
                self._stop_requested = True
                if self._status != "failed":
                    self._status = "stopped"
            else:
                self._status = "stopping"
            self._condition.notify_all()
        if worker is not None and worker is not threading.current_thread():
            worker.join(max(0.0, deadline - time.monotonic()))
        with self._condition:
            alive = worker is not None and worker.is_alive()
            remaining = 0
            # A concurrent explicit start may already own a new worker.
            if self._worker is worker:
                self._stop_requested = True
                self._shutdown_timed_out = alive
                if alive:
                    self._error_locked("shutdown_timeouts")
                elif self._status != "failed":
                    self._status = "stopped"
                remaining = len(self._outbox)
                self._condition.notify_all()
        if alive:
            self.logger.error("Alert delivery shutdown timed out; an attempt is still in flight")
        elif remaining:
            self.logger.warning(f"Alert delivery stopped with {remaining} undelivered notification(s)")
        return not alive

    def _stopping_locked(self):
        return self._stop_requested or (
            self._shutdown_deadline is not None
            and (not self._outbox or time.monotonic() >= self._shutdown_deadline)
        )

    def _run(self):
        try:
            while True:
                if self.dispatch_pending():
                    continue
                with self._condition:
                    if self._stopping_locked():
                        return
                    due = [attempt.due for delivery in self._outbox.values()
                           for attempt in delivery.channels if attempt.status == "pending"]
                    delay = max(0.0, min(due) - self._clock.monotonic()) if due else None
                    if self._inflight is not None:
                        delay = None
                    if self._shutdown_deadline is not None:
                        remaining = max(0.0, self._shutdown_deadline - time.monotonic())
                        delay = remaining if delay is None else min(delay, remaining)
                    self._condition.wait(delay)
        except BaseException as exc:
            with self._condition:
                self._worker_error = type(exc).__name__
                self._error_locked("worker_exceptions", exc)
                self._status = "failed"
                self._accepting = False
            self.logger.error(f"Alert delivery worker stopped unexpectedly ({type(exc).__name__})")
        finally:
            with self._condition:
                self._stop_requested = True
                if self._status != "failed":
                    self._status = "stopped"
                self._condition.notify_all()

    def _current_locked(self, delivery, attempt):
        # Object identity is the incident generation, including recovery/reopen races.
        return (
            self._outbox.get(delivery.key) is delivery
            and self._incidents.get(delivery.key) is delivery.incident
            and any(candidate is attempt for candidate in delivery.channels)
        )

    def _finish_locked(self, delivery):
        if any(attempt.status in ("pending", "in_flight", "unavailable")
               for attempt in delivery.channels):
            return
        self._outbox.pop(delivery.key, None)
        delivery.incident.delivery_failed = any(
            attempt.status == "failed" for attempt in delivery.channels
        )
        if delivery.channels and not delivery.incident.delivery_failed:
            self._counts["delivered"] += 1

    def dispatch_pending(self):
        """Synchronously process at most one due channel attempt, without sleeping.

        Deterministic test hook for an injected clock; not for control/timer threads.
        Returns False when idle, stopped, or another dispatch is in flight.
        """
        if not self._dispatch_lock.acquire(blocking=False):
            return False
        delivery = attempt = None
        try:
            now = self._clock.monotonic()
            with self._condition:
                if self._stopping_locked():
                    return False
                candidates = [
                    (candidate.due, item, candidate)
                    for item in self._outbox.values() for candidate in item.channels
                    if candidate.status == "pending" and candidate.due <= now
                ]
                if not candidates:
                    return False
                _, delivery, attempt = min(candidates, key=lambda item: item[0])
                if not self.enabled[delivery.alert.type]:
                    self._clear_locked(delivery.key)
                    return True
                attempt.status = "in_flight"
                self._inflight = (delivery, attempt)
            if (delivery.alert.type == AlertType.LEAK
                    and self.is_in_exclusion_window(self._clock.now())):
                with self._condition:
                    if self._current_locked(delivery, attempt):
                        self._outbox.pop(delivery.key)
                        self._counts["cancelled"] += 1
                self.logger.info("Pending leak notification cancelled by exclusion window")
                return True

            failure = None
            try:
                success = attempt.channel.send(delivery.alert) is True
            except Exception as exc:
                success = False
                failure = exc
            now = self._clock.monotonic()
            message = None
            with self._condition:
                if not self._current_locked(delivery, attempt):
                    return True
                attempt.attempts += 1
                if success:
                    attempt.status = "delivered"
                    self._counts["delivered_channels"] += 1
                else:
                    self._error_locked("failed_attempts", failure)
                    if failure is not None:
                        self._error_locked("channel_exceptions", failure)
                    if attempt.attempts >= self.MAX_ATTEMPTS:
                        attempt.status = "failed"
                        self._error_locked("retry_exhausted", failure)
                        message = ("error", "Alert channel retries exhausted after 5 attempts")
                    else:
                        attempt.status = "pending"
                        delay = 2 ** attempt.attempts
                        attempt.due = now + delay
                        self._counts["retries_scheduled"] += 1
                        message = ("warning", f"Alert channel delivery failed; retry in {delay}s")
                    if failure is not None:
                        message = (message[0], f"{message[1]} ({type(failure).__name__})")
                self._finish_locked(delivery)
            if message is not None:
                getattr(self.logger, message[0])(message[1])
            return True
        finally:
            with self._condition:
                if attempt is not None and attempt.status == "in_flight":
                    attempt.status = "pending"
                self._inflight = None
                self._condition.notify_all()
            self._dispatch_lock.release()

    def _clear_locked(self, key):
        incident = self._incidents.pop(key, None)
        if self._outbox.pop(key, None) is not None:
            self._counts["cancelled"] += 1
        return incident is not None

    def clear_alert_state(self, alert_type: AlertType, valve_name: Optional[str] = None,
                          subject: Optional[str] = None):
        """End exactly this incident and invalidate its queued/in-flight retries."""
        with self._condition:
            cleared = self._clear_locked((alert_type, valve_name, subject))
            self._condition.notify_all()
            return cleared

    def _log_occurrence(self, alert):
        log_message = f"ALERT [{alert.severity.value.upper()}] {alert.type.value}"
        if alert.valve_name:
            log_message += f" (valve: {alert.valve_name})"
        log_message += f": {alert.message}"
        if alert.severity == AlertSeverity.CRITICAL:
            self.logger.critical(log_message)
        else:
            self.logger.warning(log_message)
        if alert.data:
            self.logger.info(f"  Alert data: {alert.data}")

    def is_in_exclusion_window(self, now: datetime) -> bool:
        """Evaluate day/season/solar rules, including yesterday's overnight window.

        A None schedule time denotes a nonexistent DST occurrence and is skipped.
        A broken exclusion fails open: it must not silence a safety observation.
        Schedule helpers must be pure, local calculations.
        """
        with self._condition:
            exclusions = tuple(self.leak_detection_exclusions)
        for schedule in exclusions:
            try:
                for day in (now, now - timedelta(days=1)):
                    if not self.irrigate.shouldScheduleRun(schedule, check_date=day):
                        continue
                    start = self.irrigate.calculateScheduleTime(schedule, day)
                    if start is None:
                        continue
                    end = start + timedelta(minutes=schedule.duration)
                    reference = now
                    if start.tzinfo is not None and reference.tzinfo is None:
                        localize = getattr(start.tzinfo, "localize", None)
                        reference = localize(now) if localize else now.replace(tzinfo=start.tzinfo)
                    elif start.tzinfo is None and reference.tzinfo is not None:
                        reference = now.replace(tzinfo=None)
                    if start <= reference < end:
                        return True
            except Exception as exc:
                with self._condition:
                    self._error_locked("exclusion_errors", exc)
                self.logger.error(f"Leak exclusion evaluation failed ({type(exc).__name__})")
        return False

    def alert(self, alert_type: AlertType, message: str, valve_name: Optional[str] = None,
              data: Optional[Dict[str, Any]] = None, subject: Optional[str] = None):
        """Log and enqueue/coalesce an occurrence, without channel I/O or waiting.

        True means accepted (or log-only when no channels are configured), not
        delivered. False means disabled, excluded, coalesced or explicitly rejected.
        Leak repeat intervals use monotonic time, independent of wall-clock changes.
        """
        with self._condition:
            if not self.enabled[alert_type]:
                return False
        timestamp = self._clock.now()
        if alert_type == AlertType.LEAK and self.is_in_exclusion_window(timestamp):
            return False
        now = self._clock.monotonic()
        occurrence = Alert(alert_type, valve_name, timestamp.replace(tzinfo=None), message, deepcopy(data or {}))
        key = (alert_type, valve_name, subject)
        rejection = None
        with self._condition:
            if not self.enabled[alert_type]:
                return False
            incident = self._incidents.get(key)
            if not self._accepting:
                self._error_locked("rejected_after_shutdown")
                rejection = "Alert delivery is stopped; occurrence was not queued"
            elif incident is not None and (
                key in self._outbox or alert_type != AlertType.LEAK
                or now - incident.last_occurrence < self.leak_repeat_minutes * 60
            ):
                self._counts["coalesced"] += 1
                return False
            elif self._channel_specs and len(self._outbox) >= self._capacity:
                self._error_locked("overflow")
                self._overflowed = True
                rejection = "Alert outbox overflow; occurrence was not queued"
            elif incident is None and len(self._incidents) >= self._incident_capacity:
                self._error_locked("incident_overflow")
                self._overflowed = True
                rejection = "Alert incident capacity exhausted; occurrence was not queued"
            else:
                if incident is None:
                    incident = _Incident(now)
                    self._incidents[key] = incident
                incident.last_occurrence = now
                incident.delivery_failed = False
                if self._channel_specs:
                    self._outbox[key] = _Delivery(
                        key, incident, occurrence,
                        [_ChannelDelivery(
                            channel, now, channel_cfg,
                            status="pending" if channel is not None else "unavailable",
                        ) for channel_cfg, channel in self._channel_specs],
                    )
                self._overflowed = False
                self._condition.notify_all()
            self._counts["occurrences"] += 1
        self._log_occurrence(occurrence)
        if rejection:
            self.logger.error(rejection)
        return rejection is None

    def get_health(self):
        """Snapshot current health plus cumulative counters and historical last_error.

        Queue depth includes in-flight and unavailable notifications. Unrecovered
        incidents have a separate bound so draining the outbox never forgets them.
        No payloads, subjects, delivery IDs, or channel credentials are exposed.
        """
        with self._condition:
            pending = [attempt for delivery in self._outbox.values()
                       for attempt in delivery.channels
                       if attempt.status in ("pending", "in_flight", "unavailable")]
            retrying = sum(attempt.attempts > 0 for attempt in pending)
            unavailable = sum(attempt.status == "unavailable" for attempt in pending)
            failed = sum(incident.delivery_failed for incident in self._incidents.values())
            alive = self._worker is not None and self._worker.is_alive()
            delivery_status = (
                "in_flight" if self._inflight is not None else
                "unavailable" if unavailable else
                "retrying" if retrying else
                "pending" if pending else
                "failed" if failed or self._configuration_errors else
                "idle" if self.channels else "log_only"
            )
            return {
                "status": self._status,
                "healthy": not (self._worker_error or self._configuration_errors
                                or self._overflowed or failed or retrying
                                or self._shutdown_timed_out
                                or (self._status == "stopped" and self._outbox)),
                "worker_alive": alive,
                "accepting": self._accepting,
                "delivery_status": delivery_status,
                "queue_depth": len(self._outbox),
                "queue_capacity": self._capacity,
                "pending_channels": len(pending),
                "retrying_channels": retrying,
                "unavailable_channels": unavailable,
                "active_incidents": len(self._incidents),
                "incident_capacity": self._incident_capacity,
                "failed_incidents": failed,
                "channel_count": len(self.channels),
                "configuration_errors": self._configuration_errors,
                "last_error": self._last_error,
                "errors": dict(self._errors),
                **self._counts,
            }
