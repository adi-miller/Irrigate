import json
import logging
import threading
from datetime import datetime, timezone

import pytest

from alerts import AlertType
from clock import SystemClock
from controller import ControlError
from irrigate import Irrigate, main
from model import Job
from runtime_logging import AsyncLogHandler
from tests.runtime_support import FakeClock, make_app, make_config
from valves import TestValve, ThreeWireValve


def test_background_deadline_closes_while_notification_is_blocked(tmp_path):
  entered, release, closed = threading.Event(), threading.Event(), threading.Event()
  clock = SystemClock()

  class BlockingChannel:
    def send(self, alert):
      entered.set()
      release.wait(5)
      return True

  class ObservedValve(TestValve):
    def close(self):
      super().close()
      closed.set()

  cfg = make_config()
  cfg["alerts"]["channels"] = [{
    "type": "millerbot", "url": "https://alerts.invalid/proactive",
    "api_key": "offline-fixture", "user_id": 1, "role": "irrigation",
  }]
  path = tmp_path / "config.json"
  path.write_text(json.dumps(cfg), encoding="utf-8")
  app = Irrigate(
    str(path), offline=True, clock=clock, logger=logging.getLogger("lifecycle-test"),
    data_directory=tmp_path / "data",
    channel_factory=lambda logger, config: BlockingChannel(),
    valve_factory=lambda type, logger, config: ObservedValve(logger, config, clock),
  )
  try:
    app.start()
    app.alerts.alert(
      AlertType.MONITORING_UNAVAILABLE, "Injected notification stall", subject="resource:fixture",
    )
    assert entered.wait(2), "Fake notification was not started"
    closed.clear()
    app.controller.start_manual("Valve A", 0.002)
    assert closed.wait(1), "Blocked notification delayed the server deadline"
    assert not release.is_set()
    assert not app.valves["Valve A"].is_open
  finally:
    release.set()
    app.shutdown()
  assert all(not thread.is_alive() for thread in app._threads)


@pytest.mark.parametrize("failed_loop", ["maintenance", "control"])
def test_critical_thread_failure_is_explicit_and_closes_before_exit(tmp_path, monkeypatch, failed_loop):
  closed = threading.Event()
  clock = SystemClock()

  class ObservedValve(TestValve):
    def close(self):
      super().close()
      closed.set()

  path = tmp_path / "config.json"
  path.write_text(json.dumps(make_config()), encoding="utf-8")
  app = Irrigate(
    str(path), offline=True, clock=clock, data_directory=tmp_path / "data",
    logger=logging.getLogger("lifecycle-test"),
    valve_factory=lambda type, logger, config: ObservedValve(logger, config, clock),
  )
  try:
    app.start()
    app.controller.start_manual("Valve A")
    closed.clear()

    def fail():
      raise RuntimeError("injected scheduler failure")

    if failed_loop == "maintenance":
      monkeypatch.setattr(app, "maintenance_tick", fail)
    else:
      monkeypatch.setattr(app.controller, "tick", fail)
    assert closed.wait(2)
    assert app.terminated
    assert not app.controller.ready
    assert app.get_health()["controller"]["fault"]
  finally:
    app.shutdown()


def test_local_midnight_preserves_operation_totals_and_durable_dates(tmp_path):
  clock = FakeClock(datetime(2025, 6, 15, 23, 59, 50, tzinfo=timezone.utc))
  app = make_app(tmp_path, clock)
  try:
    app.waterflow.setLastLiter_1m(6)
    app.controller.start_manual("Valve A", 1)
    clock.advance(20)
    app.controller.tick()
    valve = app.valves["Valve A"]
    assert valve.secondsLast == 20
    assert valve.secondsDaily == 10
    assert valve.litersDaily == pytest.approx(1)
    app.controller.tick()
    app.metrics.flush()
    assert app.metrics.daily_totals("2025-06-15")["Valve A"]["seconds"] == 10
    app.controller.stop("Valve A")
  finally:
    app.shutdown()


def test_restart_does_not_restore_watering_operations(tmp_path):
  app = make_app(tmp_path)
  app.controller.start_manual("Valve A", 30)
  app.clock.advance(10)
  app.controller.tick()
  app.shutdown()
  restarted = make_app(tmp_path)
  try:
    assert restarted.controller.ready
    assert restarted.controller.operations == {}
    assert all(not valve.is_open for valve in restarted.valves.values())
    assert all([action for action, _ in valve.calls] == ["close"] for valve in restarted.valves.values())
  finally:
    restarted.shutdown()


def test_offline_composition_refuses_real_hardware_driver(tmp_path):
  cfg = make_config()
  path = tmp_path / "config.json"
  path.write_text(json.dumps(cfg), encoding="utf-8")
  app = Irrigate(
    str(path), offline=True, data_directory=tmp_path / "data",
    logger=logging.getLogger("offline-guard-test"),
    valve_factory=lambda type, logger, config: ThreeWireValve(logger, config),
  )
  with pytest.raises(ValueError, match="safe valve"):
    app.start(background=False)
  app.shutdown()


def test_shutdown_flag_prevents_new_open_immediately(tmp_path):
  app = make_app(tmp_path)
  try:
    app.exit_gracefully()
    with pytest.raises(ControlError):
      app.controller.start_manual("Valve A")
  finally:
    app.shutdown()


def test_cli_test_and_simulation_never_touch_network_or_gpio(tmp_path):
  path = tmp_path / "config.json"
  path.write_text(json.dumps(make_config()), encoding="utf-8")
  assert main(["irrigate.py", "--test", "--config", str(path)]) == 0
  assert main(["irrigate.py", "--config", str(path), "--simulate=uv:0,rain:no"]) == 0
  assert not (tmp_path / "data").exists()
  assert not (tmp_path / "log.txt").exists()


def test_both_signals_are_bound_to_the_common_shutdown_request(monkeypatch):
  import signal
  from types import SimpleNamespace
  import irrigate

  registered = []
  completed = []
  stop = threading.Event()
  stop.set()
  handler = lambda signum=None, frame=None: None
  instance = SimpleNamespace(
    exit_gracefully=handler, _stop=stop, _fatal_error=None,
    start=lambda **kwargs: True,
    shutdown=lambda reason: completed.append(reason),
  )
  monkeypatch.setattr(irrigate, "Irrigate", lambda *args, **kwargs: instance)
  monkeypatch.setattr(signal, "signal", lambda number, callback: registered.append((number, callback)))
  assert irrigate.main(["irrigate.py", "--config", "not-read.json"]) == 0
  assert registered == [(signal.SIGINT, handler), (signal.SIGTERM, handler)]
  assert completed == ["system shutdown"]


def test_blocked_log_sink_does_not_block_producer_and_overflow_is_visible():
  entered, release = threading.Event(), threading.Event()

  class BlockingSink(logging.Handler):
    def emit(self, record):
      entered.set()
      release.wait(5)

  handler = AsyncLogHandler([BlockingSink()], capacity=2)
  handler.start()
  record = logging.LogRecord("test", logging.INFO, "", 0, "offline event", (), None)
  try:
    handler.emit(record)
    assert entered.wait(1)
    for _ in range(4):
      handler.emit(record)
    health = handler.get_health()
    assert health["dropped_records"] == 2
    assert not health["available"]
  finally:
    release.set()
    assert handler.shutdown()


def test_reads_do_not_consume_weather_publication_and_failed_publish_retries(tmp_path):
  from types import SimpleNamespace
  app = make_app(tmp_path)
  messages = []
  fail_uv = True

  class Publisher:
    def publish(self, topic, payload):
      messages.append((topic, payload))
      return SimpleNamespace(rc=1 if fail_uv and topic.endswith("/uv") else 0)

    def disconnect(self):
      pass

    def loop_stop(self):
      pass

  app.mqtt.mqttClient = Publisher()
  app.mqtt.mqttStarted = True
  sensor = app.sensors["Weather"]
  try:
    for _ in range(5):
      sensor.getTelemetry(True)
    app.telemetrySensor("Weather", sensor)
    assert "Weather" not in app._sensor_cursors
    fail_uv = False
    app.telemetrySensor("Weather", sensor)
    assert app._sensor_cursors["Weather"] == sensor.revision
    assert len([topic for topic, _ in messages if topic.endswith("/uv")]) == 2
    assert len([topic for topic, _ in messages if topic.endswith("/recentPrecip")]) == 2
    sensor.getTelemetry(True)
    app.telemetrySensor("Weather", sensor)
    assert len([topic for topic, _ in messages if topic.endswith("/uv")]) == 2
  finally:
    app.shutdown()


def test_baselines_refresh_only_once_on_date_transition(tmp_path, monkeypatch):
  clock = FakeClock(datetime(2025, 6, 15, 23, 59, 59, tzinfo=timezone.utc))
  app = make_app(tmp_path, clock)
  refreshed = []
  monkeypatch.setattr(app.metrics, "load_baselines", lambda valves: refreshed.append(clock.now().date()))
  try:
    clock.advance(1)
    app.controller.tick()
    for _ in range(5):
      app.maintenance_tick()
      clock.advance(0.25)
    assert refreshed == [datetime(2025, 6, 16).date()]
  finally:
    app.shutdown()


def capture_alert_clears(app, monkeypatch):
  results = []
  original = app.alerts.clear_alert_state

  def clear(kind, valve_name=None, subject=None):
    result = original(kind, valve_name, subject)
    results.append((kind, valve_name, subject, result))
    return result

  monkeypatch.setattr(app.alerts, "clear_alert_state", clear)
  return results


def test_sensor_incident_creation_and_recovery_use_the_same_subject(tmp_path, monkeypatch):
  app = make_app(tmp_path)
  cleared = capture_alert_clears(app, monkeypatch)
  sensor = app.sensors["Weather"]
  valve = app.valves["Valve A"]
  try:
    app.waterflow.setLastLiter_1m(6)
    sensor.exception = True
    app.controller.enqueue(Job(valve, 1, valve.schedules[0]))
    app.controller.tick()
    deadline = app.controller.operations[valve.name].deadline
    assert valve.is_open
    sensor.exception = False
    app.controller.tick()
    app.maintenance_tick()
    assert (AlertType.SENSOR_ERROR, None, "Weather", True) in cleared
    assert (AlertType.MONITORING_UNAVAILABLE, None, "sensor:Weather", True) in cleared
    assert app.controller.operations[valve.name].deadline == deadline
  finally:
    app.shutdown()


def test_irregular_flow_clears_only_on_qualified_recovery_and_zero_baseline_is_safe(
    tmp_path, monkeypatch, caplog):
  app = make_app(tmp_path)
  cleared = capture_alert_clears(app, monkeypatch)
  valve = app.valves["Valve A"]
  valve.baseline_lpm, valve.baseline_std_dev = 6, 1

  def finish_short_manual(rate):
    if rate is not None:
      app.waterflow.setLastLiter_1m(rate)
    app.controller.start_manual(valve.name, 1)
    app.clock.advance(10)
    app.controller.tick()
    app.controller.stop(valve.name)
    app.maintenance_tick()

  try:
    finish_short_manual(12)
    app.clock.advance(61)
    finish_short_manual(None)
    assert not any(kind == AlertType.IRREGULAR_FLOW for kind, *_ in cleared)
    finish_short_manual(6)
    assert (AlertType.IRREGULAR_FLOW, valve.name, None, True) in cleared
    valve.baseline_lpm = 0
    app.checkIrregularFlow(valve, 60, 6)
    assert "invalid/zero baseline" in caplog.text
  finally:
    app.shutdown()
