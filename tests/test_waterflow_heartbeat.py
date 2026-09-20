import logging
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api_server
from alerts import AlertType
from controller import ControlError
from model import Job
from tests.runtime_support import FakeClock, make_app, make_config
from tests.test_controller import FaultValve, controller_fixture, namespace
from tests.test_monitoring import FakeMqttClient
from waterflows import MqttWaterflow


@pytest.fixture
def heartbeat_app(tmp_path):
  app = make_app(tmp_path, configuration=make_config(sensor=False))
  try:
    yield app
  finally:
    app.shutdown()


def flow_warnings(caplog):
  return [record.getMessage() for record in caplog.records
          if record.getMessage().startswith(
            "ALERT [WARNING] monitoring_unavailable: Monitoring unavailable: waterflow ")]


def flow_incident(app):
  return app.alerts._incidents.get((AlertType.MONITORING_UNAVAILABLE, None, "resource:waterflow"))


def test_idle_zero_heartbeats_do_not_rearm_monitoring_alerts(heartbeat_app, caplog):
  app = heartbeat_app
  for _ in range(3):
    app.waterflow.setLastLiter_1m(0)
    app._monitor_health()
    for seconds in (60, 1, 539):
      app.clock.advance(seconds)
      app.controller.tick()
      app._monitor_health()
  assert flow_warnings(caplog) == []


@pytest.mark.parametrize("age,fresh,live", [
  (0, True, True), (60, True, True), (60.001, False, True),
  (600, False, True), (660, False, True), (660.001, False, False),
])
def test_idle_liveness_does_not_change_measurement_boundaries(heartbeat_app, age, fresh, live):
  app = heartbeat_app
  flow = app.waterflow
  flow.setLastLiter_1m(0)
  original = flow.snapshot()
  history = flow.getHistory()
  app.clock.advance(age)
  sample = flow.snapshot()
  health = app.controller.get_waterflow_health()
  assert sample["available"] is fresh
  assert sample["fresh"] is fresh
  assert sample["age_seconds"] == pytest.approx(age)
  assert sample["value"] == 0
  assert sample["received"] == original["received"]
  assert sample["timestamp"] == original["timestamp"]
  assert flow.getHistory() == history
  assert health == {
    **{key: value for key, value in sample.items() if key not in ("timestamp", "received", "value")},
    "source": {"available": live, "reason": None if live else "missed idle heartbeat"},
  }


def test_missed_idle_heartbeat_warns_once_until_a_real_recovery(heartbeat_app, caplog):
  app = heartbeat_app
  app.waterflow.setLastLiter_1m(0)
  app.clock.advance(660)
  app._monitor_health()
  assert flow_incident(app) is None
  app.clock.advance(0.001)
  app._monitor_health()
  incident = flow_incident(app)
  assert incident is not None
  for seconds in (0, 60, 600, 3600):
    app.clock.advance(seconds)
    app.controller.tick()
    app.waterflow.connected = False
    app._monitor_health()
    app.waterflow.connected = True
    app.waterflow.start()
    app.controller.stop("Valve A")
    app.controller.start_manual("Valve A", 1)
    app._monitor_health()
    app.controller.stop("Valve A")
    app._monitor_health()
    app.get_health()
    assert flow_incident(app) is incident
  assert len(flow_warnings(caplog)) == 1
  assert "SensorErr" in app._tempStatus
  app.waterflow.setLastLiter_1m(0)
  app._monitor_health()
  assert flow_incident(app) is None
  assert "SensorErr" not in app._tempStatus
  app.clock.advance(660.001)
  app._monitor_health()
  assert len(flow_warnings(caplog)) == 2


def test_first_idle_heartbeat_wait_is_bounded_and_cannot_be_restarted(heartbeat_app, caplog):
  app = heartbeat_app
  start = app.clock.monotonic()
  for seconds in (60, 540, 60):
    app.clock.advance(seconds)
    assert app.controller.reconcile_startup()
    app.waterflow.connected = False
    assert app.controller.get_waterflow_health()["source"]["reason"] == "disconnected"
    app.waterflow.connected = True
    app.waterflow.start()
    app.waterflow.enabled = False
    app.waterflow.enabled = True
    app.controller.tick()
    app._monitor_health()
    assert app.controller._waterflow_startup_since == start
    assert app.controller.get_waterflow_health()["source"]["available"]
    assert app.waterflow.snapshot()["received"] is None
    assert app.waterflow.snapshot()["value"] is None
    assert not app.waterflow.snapshot()["fresh"]
    assert app.waterflow.getHistory() == []
  app.clock.advance(0.001)
  for _ in range(3):
    assert app.controller.reconcile_startup()
    app.waterflow.start()
    app._monitor_health()
    assert not app.controller.get_waterflow_health()["source"]["available"]
  assert len(flow_warnings(caplog)) == 1
  intervals = app.waterflow.intervals(start, app.clock.monotonic())
  assert all(value is None for _, _, value in intervals)
  assert sum(right - left for left, right, _ in intervals) == pytest.approx(660.001)
  app.waterflow.setLastLiter_1m(0)
  app._monitor_health()
  assert flow_incident(app) is None


@pytest.mark.parametrize("flag", ["connected", "started"])
def test_idle_grace_does_not_hide_disconnection(heartbeat_app, caplog, flag):
  app = heartbeat_app
  app.waterflow.setLastLiter_1m(0)
  app.clock.advance(120)
  setattr(app.waterflow, flag, False)
  app._monitor_health()
  assert app.controller.get_waterflow_health()["source"] == {
    "available": False, "reason": "disconnected",
  }
  assert len(flow_warnings(caplog)) == 1
  setattr(app.waterflow, flag, True)
  app._monitor_health()
  assert flow_incident(app) is None
  app.clock.advance(541)
  app._monitor_health()
  assert len(flow_warnings(caplog)) == 2


@pytest.mark.parametrize("observed", [False, True])
def test_opening_from_idle_has_only_sixty_seconds_for_a_real_report(heartbeat_app, caplog, observed):
  app = heartbeat_app
  if observed:
    app.waterflow.setLastLiter_1m(0)
  app.clock.advance(600)
  operation = app.controller.start_manual("Valve A", 2)
  deadline = operation.deadline
  app._monitor_health()
  assert flow_incident(app) is None
  assert app.controller.get_waterflow_health()["source"]["available"]
  assert not app.waterflow.snapshot()["available"]
  app.clock.advance(60)
  app.controller.tick()
  app._monitor_health()
  assert flow_incident(app) is None
  app.clock.advance(0.001)
  app.controller.tick()
  app._monitor_health()
  assert len(flow_warnings(caplog)) == 1
  assert not app.controller.get_waterflow_health()["source"]["available"]
  assert not operation.no_flow
  assert operation.deadline == deadline
  app.clock.advance(deadline - app.clock.monotonic())
  app.controller.tick()
  app._monitor_health()
  assert not app.valves["Valve A"].is_open
  assert operation.open_seconds == pytest.approx(120)
  assert operation.liters == 0
  assert not operation.complete
  assert app.metrics.get_health()["unavailable_seconds"] == pytest.approx(120)
  assert len(flow_warnings(caplog)) == 1


@pytest.mark.parametrize("rate", [0, 6])
def test_active_reports_replace_opening_grace_with_strict_freshness(heartbeat_app, caplog, rate):
  app = heartbeat_app
  app.waterflow.setLastLiter_1m(0)
  app.clock.advance(300)
  operation = app.controller.start_manual("Valve A", 5)
  deadline = operation.deadline
  for _ in range(3):
    app.clock.advance(10)
    app.waterflow.setLastLiter_1m(rate)
    app.controller.tick()
    app._monitor_health()
  app.clock.advance(60)
  assert app.controller.get_waterflow_health()["source"]["available"]
  assert app.waterflow.snapshot()["fresh"]
  app.clock.advance(0.001)
  app.controller.tick()
  app._monitor_health()
  assert not app.controller.get_waterflow_health()["source"]["available"]
  assert not app.waterflow.snapshot()["fresh"]
  assert len(flow_warnings(caplog)) == 1
  assert operation.deadline == deadline


def test_positive_flow_after_close_never_gains_idle_or_opening_grace(heartbeat_app, caplog):
  app = heartbeat_app
  app.waterflow.setLastLiter_1m(6)
  app.controller.start_manual("Valve A", 5)
  app.clock.advance(10)
  app.controller.stop("Valve A")
  app.clock.advance(50)
  assert app.controller.get_waterflow_health()["source"]["available"]
  app.clock.advance(0.001)
  app._monitor_health()
  assert not app.controller.get_waterflow_health()["source"]["available"]
  app.controller.start_manual("Valve A", 5)
  app._monitor_health()
  assert not app.controller.get_waterflow_health()["source"]["available"]
  assert len(flow_warnings(caplog)) == 1


def test_real_zero_before_close_allows_normal_idle_heartbeat_cadence(heartbeat_app):
  app = heartbeat_app
  app.waterflow.setLastLiter_1m(0)
  app.controller.start_manual("Valve A", 5)
  app.clock.advance(10)
  app.waterflow.setLastLiter_1m(0)
  app.controller.stop("Valve A")
  app.clock.advance(600)
  assert app.controller.get_waterflow_health()["source"]["available"]
  assert not app.waterflow.snapshot()["available"]


def test_sensor_paused_and_queued_ownership_is_idle_but_open_is_not():
  controller, clock, valves, sensor, flow, _ = controller_fixture(count=2)
  flow.setLastLiter_1m(0)
  sensor.disable = True
  for valve in valves.values():
    controller.enqueue(Job(valve, 20, valve.schedules[0]))
  controller.tick()
  clock.advance(600)
  controller.tick()
  deadline = controller.operations["Valve A"].deadline
  assert valves["Valve A"].handled
  assert controller.states["Valve A"] == "paused"
  assert controller.q.qsize() == 1
  assert all(not valve.is_open for valve in valves.values())
  assert controller.get_waterflow_health()["source"]["available"]
  assert not flow.snapshot()["available"]
  sensor.disable = False
  controller.tick()
  assert valves["Valve A"].is_open
  clock.advance(60)
  assert controller.get_waterflow_health()["source"]["available"]
  clock.advance(0.001)
  controller.tick()
  assert not controller.get_waterflow_health()["source"]["available"]
  assert controller.operations["Valve A"].deadline == deadline


def test_pause_resume_and_close_cannot_renew_an_unobserved_opening():
  controller, clock, valves, sensor, flow, _ = controller_fixture()
  flow.setLastLiter_1m(0)
  clock.advance(120)
  valve = valves["Valve A"]
  controller.enqueue(Job(valve, 10, valve.schedules[0]))
  controller.tick()
  deadline = controller.operations[valve.name].deadline
  for disabled in (True, False, True):
    clock.advance(20)
    sensor.disable = disabled
    controller.tick()
    assert controller.get_waterflow_health()["source"]["available"]
  clock.advance(0.001)
  controller.tick()
  assert controller.states[valve.name] == "paused"
  assert not controller.get_waterflow_health()["source"]["available"]
  sensor.disable = False
  controller.tick()
  assert valve.is_open
  assert not controller.get_waterflow_health()["source"]["available"]
  assert controller.operations[valve.name].deadline == deadline


def test_another_open_and_repeated_close_open_do_not_extend_the_first_report_deadline():
  controller, clock, _, _, flow, _ = controller_fixture(count=2)
  flow.setLastLiter_1m(0)
  clock.advance(120)
  controller.start_manual("Valve A", 5)
  clock.advance(50)
  controller.start_manual("Valve B", 5)
  clock.advance(10.001)
  assert not controller.get_waterflow_health()["source"]["available"]
  controller.stop("Valve A")
  assert not controller.get_waterflow_health()["source"]["available"]
  controller.stop("Valve B")
  for _ in range(3):
    controller.start_manual("Valve A", 5)
    assert not controller.get_waterflow_health()["source"]["available"]
    controller.stop("Valve A")
    assert not controller.get_waterflow_health()["source"]["available"]
  flow.setLastLiter_1m(0)
  clock.advance(120)
  controller.start_manual("Valve A", 5)
  assert controller.get_waterflow_health()["source"]["available"]


@pytest.mark.parametrize("state,is_open,fault", [
  ("unknown", False, None), ("fault", False, "close failed"),
  ("closed", True, None), ("open", False, None),
  ("paused", True, None), ("unexpected", False, None),
])
def test_uncertain_or_inconsistent_actuator_state_never_gets_idle_tolerance(state, is_open, fault):
  controller, clock, valves, _, flow, _ = controller_fixture()
  flow.setLastLiter_1m(0)
  clock.advance(61)
  controller.states["Valve A"] = state
  controller.faults["Valve A"] = fault
  valves["Valve A"].is_open = is_open
  assert not controller.get_waterflow_health()["source"]["available"]


@pytest.mark.parametrize("action", ["open", "close"])
def test_actual_actuator_failure_cancels_idle_and_opening_tolerance(action):
  controller, clock, valves, _, flow, _ = controller_fixture()
  flow.setLastLiter_1m(0)
  clock.advance(120)
  valve = valves["Valve A"]
  if action == "open":
    valve.fail_open = True
    with pytest.raises(ControlError):
      controller.start_manual(valve.name, 5)
  else:
    controller.start_manual(valve.name, 5)
    valve.fail_close = 3
    with pytest.raises(ControlError):
      controller.stop(valve.name)
  assert not controller.ready
  assert valve.is_open
  assert not controller.get_waterflow_health()["source"]["available"]
  flow.setLastLiter_1m(0)
  clock.advance(61)
  assert not controller.get_waterflow_health()["source"]["available"]


def test_failed_startup_does_not_grant_an_awaiting_first_heartbeat_window(tmp_path):
  clock = FakeClock()

  def factory(kind, logger, config):
    valve = FaultValve(logger, config, clock)
    valve.fail_close = 3
    return valve

  app = make_app(tmp_path, clock=clock, configuration=make_config(sensor=False), valve_factory=factory)
  try:
    assert not app.controller.ready
    assert app.controller._waterflow_startup_since is None
    assert not app.controller.get_waterflow_health()["source"]["available"]
    app.controller.stop("Valve A")
    assert app.controller.ready
    assert not app.controller.get_waterflow_health()["source"]["available"]
    app.waterflow.setLastLiter_1m(0)
    assert app.controller.get_waterflow_health()["source"]["available"]
  finally:
    app.shutdown()


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("payload", [b"", b"bad", b"NaN", b"Infinity", b"-1", b"1e999", b"{}"])
def test_invalid_mqtt_reports_do_not_refresh_or_recover_source_health(observed, payload, caplog):
  clock = FakeClock()
  startup = clock.monotonic()
  client = FakeMqttClient()
  flow = MqttWaterflow(
    logging.getLogger("heartbeat-mqtt-test"), namespace(make_config()["waterflow"]),
    clock, client_factory=lambda: client,
  )
  flow.start()
  flow.on_connect(client, None, {}, 0)
  try:
    if observed:
      flow.on_message(client, None, SimpleNamespace(payload=b"0", topic=flow.config.topic))
    clock.advance(600)
    before, history = flow.snapshot(), flow.getHistory()
    assert flow.get_health(idle=True, startup_since=startup)["source"]["available"]
    flow.on_message(client, None, SimpleNamespace(payload=payload, topic=flow.config.topic))
    assert "Invalid waterflow reading" in caplog.text
    assert flow.snapshot() == before
    assert flow.getHistory() == history
    assert flow.get_health(idle=True, startup_since=startup)["source"] == {
      "available": False, "reason": "invalid reading",
    }
    flow.on_disconnect(client, None, 1)
    assert flow.get_health(idle=True, startup_since=startup)["source"]["reason"] == "disconnected"
    flow.on_connect(client, None, {}, 0)
    flow.start()
    flow.expect_active(startup_since=startup)
    assert not flow.get_health(active=True)["source"]["available"]
    clock.advance(61)
    assert not flow.get_health(idle=True, startup_since=startup)["source"]["available"]
    assert flow.snapshot()["received"] == before["received"]
    flow.on_message(client, None, SimpleNamespace(payload=b"0", topic=flow.config.topic))
    assert flow.get_health(idle=True, startup_since=startup)["source"]["available"]
    assert flow.snapshot()["received"] == clock.monotonic()
  finally:
    assert flow.shutdown()


@pytest.mark.parametrize("observed", [False, True])
def test_api_reads_cannot_refresh_measurements_timers_or_incidents(heartbeat_app, monkeypatch, caplog, observed):
  app = heartbeat_app
  flow = app.waterflow
  if observed:
    flow.setLastLiter_1m(0)
  original = flow.snapshot()
  history = flow.getHistory()
  start = app.clock.monotonic()
  monkeypatch.setattr(api_server, "irrigate_instance", app)
  api_server.invalidate_next_runs_cache()
  try:
    with TestClient(api_server.app) as client:
      for age in (60, 600, 660):
        app.clock.advance(start + age - app.clock.monotonic())
        for _ in range(3):
          status = client.get("/api/status")
          health = client.get("/api/health")
          assert status.status_code == health.status_code == 200
          data = status.json()["waterflow"]
          assert data["flow_rate_lpm"] == 0
          assert data["last_update"] == (original["timestamp"].isoformat() if observed else None)
          assert data["history"] == history
          measure = health.json()["monitoring"]["waterflow"]
          assert measure["fresh"] is (observed and age <= 60)
          assert measure["source"]["available"]
      app.clock.advance(0.001)
      assert not client.get("/api/health").json()["monitoring"]["waterflow"]["source"]["available"]
      assert flow_incident(app) is None
      app._monitor_health()
      incident = flow_incident(app)
      counters = app.alerts.get_health()
      for _ in range(3):
        client.get("/api/status")
        client.get("/api/health")
      assert app.alerts.get_health() == counters
      assert flow_incident(app) is incident
      assert len(flow_warnings(caplog)) == 1
    assert flow.snapshot()["received"] == original["received"]
    assert flow.snapshot()["timestamp"] == original["timestamp"]
    assert flow.getHistory() == history
    assert flow._opening_deadline is None
    assert app.controller._waterflow_startup_since == start
  finally:
    api_server.invalidate_next_runs_cache()


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("jump", [-86400, 604800])
def test_idle_heartbeat_uses_monotonic_not_wall_time(heartbeat_app, observed, jump):
  app = heartbeat_app
  if observed:
    app.waterflow.setLastLiter_1m(0)
  app.clock.advance(600, wall_seconds=jump)
  assert app.controller.get_waterflow_health()["source"]["available"]
  app.clock.advance(60, wall_seconds=-jump)
  assert app.controller.get_waterflow_health()["source"]["available"]
  app.clock.advance(0.001, wall_seconds=jump)
  assert not app.controller.get_waterflow_health()["source"]["available"]


@pytest.mark.parametrize("jump", [-86400, 604800])
def test_opening_grace_and_watering_deadline_ignore_wall_clock_jumps(heartbeat_app, jump):
  app = heartbeat_app
  app.waterflow.setLastLiter_1m(0)
  app.clock.advance(600)
  operation = app.controller.start_manual("Valve A", 5)
  deadline = operation.deadline
  app.clock.advance(60, wall_seconds=jump)
  assert app.controller.get_waterflow_health()["source"]["available"]
  app.clock.advance(0.001, wall_seconds=-jump)
  assert not app.controller.get_waterflow_health()["source"]["available"]
  assert operation.deadline == deadline


def test_idle_source_health_cannot_turn_stale_zero_into_data_or_resolve_a_leak(heartbeat_app):
  app = heartbeat_app
  flow = app.waterflow
  app.clock.advance(60)
  flow.setLastLiter_1m(2)
  app._check_leak()
  assert "Leaking" in app._tempStatus
  flow.setLastLiter_1m(0)
  start = app.clock.monotonic()
  app.clock.advance(600)
  assert app.controller.get_waterflow_health()["source"]["available"]
  assert not flow.snapshot()["available"]
  assert flow.intervals(start, start + 600) == [
    (start, start + 60, 0), (start + 60, start + 600, None),
  ]
  app._monitor_health()
  app._check_leak()
  assert "Leaking" in app._tempStatus
  flow.setLastLiter_1m(0)
  app._check_leak()
  assert "Leaking" not in app._tempStatus


def test_rolling_no_flow_still_needs_fresh_observations(heartbeat_app, caplog):
  app = heartbeat_app
  flow = app.waterflow
  flow.setLastLiter_1m(0)
  operation = app.controller.start_manual("Valve A", 5)
  for _ in range(6):
    app.clock.advance(10)
    flow.setLastLiter_1m(0)
    app.controller.tick()
    app._monitor_health()
  assert operation.no_flow
  assert not flow_warnings(caplog)
  key = (AlertType.MALFUNCTION_NO_FLOW, "Valve A", None)
  incident = app.alerts._incidents[key]
  flow.connected = False
  app.clock.advance(61)
  app.controller.tick()
  assert app.alerts._incidents[key] is incident
  assert operation.last_positive is None
  assert not operation.complete
  flow.connected = True
  flow.setLastLiter_1m(6)
  app.clock.advance(1)
  app.controller.tick()
  assert not operation.no_flow
  assert key not in app.alerts._incidents


def test_flow_reception_never_needs_the_controller_lock(heartbeat_app):
  app = heartbeat_app
  received = threading.Event()

  def receive():
    app.waterflow.connected = False
    app.waterflow.connected = True
    app.waterflow.setLastLiter_1m(0)
    received.set()

  with app.controller.lock:
    worker = threading.Thread(target=receive)
    worker.start()
    completed = received.wait(1)
  worker.join(1)
  assert completed, "Flow callback waited for the controller lock"
  assert not worker.is_alive()
  assert app.controller.get_waterflow_health()["source"]["available"]
