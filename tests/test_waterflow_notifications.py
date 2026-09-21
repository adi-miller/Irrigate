from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api_server
from alerts import AlertType
from model import Job
from tests.runtime_support import FakeClock, make_app, make_config
from tests.test_alert_delivery import FakeChannel
from tests.test_controller import FaultValve
from tests.test_monitoring import FakeMqttClient
from tests.test_waterflow_heartbeat import flow_incident
from waterflows import MqttWaterflow


@pytest.fixture
def notification_app_factory(tmp_path):
  apps = []

  def make(configuration=None, **kwargs):
    configuration = make_config() if configuration is None else configuration
    configuration["alerts"]["channels"] = [{
      "type": "millerbot", "url": "https://alerts.invalid/proactive",
      "api_key": "offline-fixture", "user_id": 1, "role": "irrigation",
    }]
    channel = FakeChannel()
    directory = tmp_path / str(len(apps))
    directory.mkdir()
    app = make_app(
      directory, configuration=configuration,
      channel_factory=lambda logger, config: channel, **kwargs,
    )
    apps.append(app)
    return app, channel

  yield make
  for app in apps:
    assert app.shutdown()


def dispatch(app):
  while app.alerts.dispatch_pending():
    pass


def monitor(app):
  app._monitor_health()
  dispatch(app)


def flow_notifications(channel):
  return [alert for alert in channel.calls
          if alert.type == AlertType.MONITORING_UNAVAILABLE and alert.data.get("source") == "waterflow"]


@pytest.mark.parametrize("value", [None, 0, 2])
@pytest.mark.parametrize("state", ["idle", "active", "uncertain"])
@pytest.mark.parametrize("age", [60, 60.001, 600, 660, 660.001])
def test_notification_boundaries_are_independent_of_value_and_actuator_state(
    notification_app_factory, value, state, age):
  app, channel = notification_app_factory()
  flow = app.waterflow
  if value is not None:
    flow.setLastLiter_1m(value)
  if state == "active":
    app.controller.start_manual("Valve A", 30)
  elif state == "uncertain":
    app.controller.states["Valve A"] = "unknown"
    app.valves["Valve A"].is_open = True
  original, history = flow.snapshot(), flow.getHistory()
  app.clock.advance(age)
  monitor(app)
  sample = flow.snapshot()
  health = app.controller.get_waterflow_health()
  assert sample["fresh"] is (value is not None and age <= 60)
  assert sample["available"] is sample["fresh"]
  assert sample["age_seconds"] == (None if value is None else pytest.approx(age))
  for key in ("value", "received", "timestamp"):
    assert sample[key] == original[key]
  assert flow.getHistory() == history
  assert ("SensorErr" in app._tempStatus) is (not health["source"]["available"])
  notifications = flow_notifications(channel)
  assert len(notifications) == int(age > 660)
  assert (flow_incident(app) is not None) is (age > 660)
  if notifications:
    alert = notifications[0]
    reason = "no valid reading" if value is None else "stale reading"
    assert alert.to_dict() == {
      "type": "monitoring_unavailable", "severity": "warning", "valve_name": None,
      "timestamp": app.clock.now().replace(tzinfo=None).isoformat(),
      "message": "Monitoring unavailable: waterflow (%s)" % reason,
      "data": {"source": "waterflow", "reason": reason},
    }


@pytest.mark.parametrize("jitter", [0, 10, 59.999, 60])
def test_physical_watering_positive_tail_and_idle_heartbeats_do_not_warn(
    notification_app_factory, jitter):
  app, channel = notification_app_factory()
  flow = app.waterflow
  commands = list(app.valves["Valve A"].calls)
  flow.setLastLiter_1m(0)
  for rate in (6, 7, 5, 4, 2):
    app.clock.advance(10)
    flow.setLastLiter_1m(rate)
    monitor(app)
  last_positive = flow.snapshot()
  app.clock.advance(60.044)
  monitor(app)
  assert not app.controller.get_waterflow_health()["source"]["available"]
  assert not flow.snapshot()["fresh"]
  assert flow.snapshot()["value"] == 2
  assert flow.snapshot()["timestamp"] == last_positive["timestamp"]
  assert flow_notifications(channel) == []
  app.clock.advance(600 + jitter - 60.044)
  monitor(app)
  assert flow_notifications(channel) == []
  flow.setLastLiter_1m(0)
  monitor(app)
  for _ in range(3):
    app.clock.advance(60.001)
    monitor(app)
    app.clock.advance(600 + jitter - 60.001)
    monitor(app)
    assert flow_notifications(channel) == []
    flow.setLastLiter_1m(0)
    monitor(app)
  assert app.controller.operations == {}
  assert app.valves["Valve A"].calls == commands


def test_active_idle_health_flapping_cannot_create_repeated_early_incidents(notification_app_factory):
  app, channel = notification_app_factory()
  app.waterflow.setLastLiter_1m(0)
  app.controller.start_manual("Valve A", 5)
  app.clock.advance(10)
  app.waterflow.setLastLiter_1m(0)
  received = app.clock.monotonic()
  for _ in range(3):
    app.clock.advance(61)
    app.controller.tick()
    monitor(app)
    app.controller.stop("Valve A")
    monitor(app)
    app.controller.start_manual("Valve A", 5)
    monitor(app)
  assert flow_notifications(channel) == []
  app.clock.advance(received + 660 - app.clock.monotonic())
  monitor(app)
  assert flow_notifications(channel) == []
  app.clock.advance(0.001)
  monitor(app)
  incident = flow_incident(app)
  for _ in range(3):
    app.controller.stop("Valve A")
    monitor(app)
    app.controller.start_manual("Valve A", 5)
    monitor(app)
    assert flow_incident(app) is incident
  assert len(flow_notifications(channel)) == 1


def test_opening_grace_cannot_delay_or_clear_an_overdue_notification(notification_app_factory):
  app, channel = notification_app_factory()
  app.waterflow.setLastLiter_1m(0)
  app.clock.advance(659)
  app.controller.start_manual("Valve A", 2)
  app.clock.advance(1)
  monitor(app)
  assert flow_notifications(channel) == []
  app.clock.advance(0.001)
  assert app.controller.get_waterflow_health()["source"]["available"]
  monitor(app)
  incident = flow_incident(app)
  assert incident is not None
  for _ in range(3):
    app.controller.stop("Valve A")
    monitor(app)
    app.controller.start_manual("Valve A", 2)
    assert app.controller.get_waterflow_health()["source"]["available"]
    monitor(app)
    assert flow_incident(app) is incident
  app.clock.advance(60)
  monitor(app)
  assert not app.controller.get_waterflow_health()["source"]["available"]
  assert flow_incident(app) is incident
  assert len(flow_notifications(channel)) == 1


@pytest.mark.parametrize("value", [None, 0, 2])
def test_six_hour_outage_notifies_once_across_reconnects_pauses_and_completion(
    notification_app_factory, value):
  app, channel = notification_app_factory()
  if value is not None:
    app.waterflow.setLastLiter_1m(value)
  app.clock.advance(661)
  monitor(app)
  incident = flow_incident(app)
  assert incident is not None
  valve, sensor = app.valves["Valve A"], app.sensors["Weather"]
  original = app.waterflow.snapshot()
  for _ in range(6):
    app.waterflow.connected = False
    monitor(app)
    app.waterflow.connected = True
    app.waterflow.start()
    monitor(app)
    assert app.controller.reconcile_startup()
    app.controller.start_manual(valve.name, 1)
    monitor(app)
    app.controller.stop(valve.name)
    monitor(app)
    sensor.disable = True
    app.controller.enqueue(Job(valve, 1, valve.schedules[0]))
    app.controller.tick()
    assert app.controller.states[valve.name] == "paused"
    monitor(app)
    sensor.disable = False
    app.controller.tick()
    monitor(app)
    sensor.disable = True
    app.controller.tick()
    monitor(app)
    app.clock.advance(60)
    app.controller.tick()
    assert not valve.handled
    monitor(app)
    app.clock.advance(3540)
    app.controller.tick()
    app.get_health()
    app.waterflow.getHistory()
    monitor(app)
    assert flow_incident(app) is incident
    assert app.waterflow.snapshot()["received"] == original["received"]
  assert len(flow_notifications(channel)) == 1
  assert app.alerts.get_health()["occurrences"] == 1


@pytest.mark.parametrize("value", [0, 2])
@pytest.mark.parametrize("poll_recovery", [False, True])
def test_genuine_recovery_rearms_even_if_the_next_poll_is_already_stale(
    notification_app_factory, value, poll_recovery):
  app, channel = notification_app_factory()
  app.waterflow.setLastLiter_1m(2)
  app.clock.advance(661)
  monitor(app)
  first = flow_incident(app)
  app.clock.advance(10)
  app.waterflow.setLastLiter_1m(value)
  if poll_recovery:
    monitor(app)
    assert flow_incident(app) is None
    app.clock.advance(660)
    monitor(app)
    assert len(flow_notifications(channel)) == 1
    app.clock.advance(0.001)
  else:
    app.clock.advance(660.001)
  monitor(app)
  assert flow_incident(app) is not first
  assert len(flow_notifications(channel)) == 2
  app.clock.advance(7200)
  for _ in range(3):
    monitor(app)
  assert len(flow_notifications(channel)) == 2


def test_invalid_mqtt_payloads_and_reconnects_cannot_recover_a_missing_reading(notification_app_factory):
  clock, client = FakeClock(), FakeMqttClient()

  def factory(kind, logger, config):
    return MqttWaterflow(logger, config, clock, client_factory=lambda: client)

  app, channel = notification_app_factory(clock=clock, waterflow_factory=factory)
  flow = app.waterflow
  flow.start()
  flow.on_connect(client, None, {}, 0)
  flow.on_message(client, None, SimpleNamespace(payload=b"2", topic=flow.config.topic))
  clock.advance(661)
  monitor(app)
  incident = flow_incident(app)
  original, history = flow.snapshot(), flow.getHistory()
  for payload in (b"bad", b"NaN", b"-1", b"Infinity", b"{}", b""):
    flow.on_message(client, None, SimpleNamespace(payload=payload, topic=flow.config.topic))
    monitor(app)
    assert app.controller.get_waterflow_health()["source"]["reason"] == "invalid reading"
    flow.on_disconnect(client, None, 1)
    monitor(app)
    flow.on_connect(client, None, {}, 0)
    flow.start()
    monitor(app)
    assert flow_incident(app) is incident
    assert flow.snapshot() == original
    assert flow.getHistory() == history
  assert len(flow_notifications(channel)) == 1
  flow.on_message(client, None, SimpleNamespace(payload=b"0", topic=flow.config.topic))
  monitor(app)
  assert flow_incident(app) is None
  clock.advance(661)
  monitor(app)
  assert len(flow_notifications(channel)) == 2


@pytest.mark.parametrize("failure", ["connected", "started", "invalid reading"])
def test_specific_flow_failures_remain_immediate(notification_app_factory, failure):
  app, channel = notification_app_factory()
  app.waterflow.setLastLiter_1m(0)
  app.clock.advance(30)
  if failure == "invalid reading":
    with pytest.raises(ValueError):
      app.waterflow.setLastLiter_1m("bad")
  else:
    setattr(app.waterflow, failure, False)
  monitor(app)
  reason = "invalid reading" if failure == "invalid reading" else "disconnected"
  assert app.controller.get_waterflow_health()["source"] == {"available": False, "reason": reason}
  assert len(flow_notifications(channel)) == 1
  assert flow_notifications(channel)[0].data["reason"] == reason
  if failure == "invalid reading":
    app.waterflow.connected = False
    monitor(app)
    app.waterflow.connected = True
    monitor(app)
    assert len(flow_notifications(channel)) == 1
    app.waterflow.setLastLiter_1m(0)
  else:
    setattr(app.waterflow, failure, True)
  monitor(app)
  assert flow_incident(app) is None


@pytest.mark.parametrize("value", [None, 0, 2])
def test_deferring_staleness_does_not_recover_a_disconnection_with_unhealthy_source(
    notification_app_factory, value):
  app, channel = notification_app_factory()
  if value is not None:
    app.waterflow.setLastLiter_1m(value)
  app.controller.states["Valve A"] = "unknown"
  app.valves["Valve A"].is_open = True
  app.clock.advance(61)
  app.waterflow.connected = False
  monitor(app)
  incident = flow_incident(app)
  assert incident is not None
  app.waterflow.connected = True
  monitor(app)
  assert not app.controller.get_waterflow_health()["source"]["available"]
  assert flow_incident(app) is incident
  for seconds in (599, 0.001, 3600):
    app.clock.advance(seconds)
    monitor(app)
    assert flow_incident(app) is incident
  assert len(flow_notifications(channel)) == 1


def test_notification_state_is_pure_and_recovery_counts_observations_not_timestamps(notification_app_factory):
  app, _ = notification_app_factory()
  flow = app.waterflow

  def state():
    return flow.get_notification_state(startup_since=app._start_mono)

  assert state()["reading_revision"] == 0
  flow.setLastLiter_1m(0)
  first, sample, history = state(), flow.snapshot(), flow.getHistory()
  for _ in range(3):
    assert state() == first
    flow.get_health()
    app.get_health()
    assert flow.snapshot() == sample
    assert flow.getHistory() == history
  with pytest.raises(ValueError):
    flow.setLastLiter_1m("bad")
  assert state()["reason"] == "invalid reading"
  assert state()["reading_revision"] == first["reading_revision"]
  flow.connected = False
  flow.connected = True
  flow.start()
  assert state()["reading_revision"] == first["reading_revision"]
  flow.setLastLiter_1m(0)
  assert flow.snapshot() == sample
  assert flow.getHistory() == history
  assert state()["reason"] is None
  assert state()["reading_revision"] == first["reading_revision"] + 1


@pytest.mark.parametrize("configured", [False, True])
def test_disabled_or_unconfigured_meter_does_not_notify(notification_app_factory, configured):
  configuration = make_config()
  if configured:
    configuration["waterflow"]["enabled"] = False
  else:
    configuration.pop("waterflow")
  app, channel = notification_app_factory(configuration=configuration)
  for _ in range(3):
    app.clock.advance(7200)
    monitor(app)
    assert not app.get_health()["monitoring"]["waterflow"]["enabled"]
  assert flow_notifications(channel) == []
  assert flow_incident(app) is None
  assert "SensorErr" not in app._tempStatus


@pytest.mark.parametrize("delivered", [False, True])
def test_disabling_the_meter_is_not_sample_recovery(notification_app_factory, delivered):
  app, channel = notification_app_factory()
  channel.default = delivered
  app.waterflow.setLastLiter_1m(0)
  app.clock.advance(661)
  monitor(app)
  incident = flow_incident(app)
  app.waterflow.enabled = False
  monitor(app)
  assert "SensorErr" not in app._tempStatus
  app.clock.advance(7200)
  monitor(app)
  app.waterflow.enabled = True
  monitor(app)
  assert flow_incident(app) is incident
  assert len(flow_notifications(channel)) == (1 if delivered else 2)
  assert len({id(alert) for alert in flow_notifications(channel)}) == 1
  assert app.alerts.get_health()["occurrences"] == 1


def test_first_sample_notification_uses_a_stable_anchor_even_after_failed_startup(notification_app_factory):
  clock = FakeClock()

  def factory(kind, logger, config):
    valve = FaultValve(logger, config, clock)
    valve.fail_close = 3
    return valve

  app, channel = notification_app_factory(clock=clock, valve_factory=factory)
  start = app._start_mono
  assert not app.controller.ready
  monitor(app)
  assert any(alert.type == AlertType.ACTUATION_FAILURE for alert in channel.calls)
  assert flow_notifications(channel) == []
  for age in (60.001, 600):
    clock.advance(start + age - clock.monotonic())
    monitor(app)
    assert not app.controller.get_waterflow_health()["source"]["available"]
    assert flow_notifications(channel) == []
  app.controller.stop("Valve A")
  assert app.controller.ready
  for _ in range(3):
    app.waterflow.connected = False
    app.waterflow.connected = True
    app.waterflow.start()
    assert app.controller.reconcile_startup()
    app.controller.start_manual("Valve A", 1)
    app.controller.stop("Valve A")
    app.get_health()
    monitor(app)
  assert app._start_mono == start
  assert app.controller._waterflow_startup_since is None
  clock.advance(60)
  monitor(app)
  assert flow_notifications(channel) == []
  clock.advance(0.001)
  monitor(app)
  assert len(flow_notifications(channel)) == 1
  assert app.waterflow.snapshot()["value"] is None
  assert app.waterflow.getHistory() == []


@pytest.mark.parametrize("value", [None, 0, 2])
@pytest.mark.parametrize("jump", [-86400, 604800])
def test_notification_threshold_and_incident_ignore_wall_clock_jumps(
    notification_app_factory, value, jump):
  app, channel = notification_app_factory()
  if value is not None:
    app.waterflow.setLastLiter_1m(value)
  app.clock.advance(60.001, wall_seconds=jump)
  monitor(app)
  assert flow_notifications(channel) == []
  app.clock.advance(599.999, wall_seconds=-jump)
  monitor(app)
  assert flow_notifications(channel) == []
  app.clock.advance(0.001, wall_seconds=jump)
  monitor(app)
  incident = flow_incident(app)
  app.clock.advance(0, wall_seconds=-jump)
  monitor(app)
  assert flow_incident(app) is incident
  assert len(flow_notifications(channel)) == 1


@pytest.mark.parametrize("value", [None, 0, 2])
def test_api_reads_neither_notify_nor_consume_genuine_recovery(
    notification_app_factory, monkeypatch, value):
  app, channel = notification_app_factory()
  flow = app.waterflow
  if value is not None:
    flow.setLastLiter_1m(value)
  original, history = flow.snapshot(), flow.getHistory()
  start, counters = app._start_mono, app.alerts.get_health()
  monkeypatch.setattr(api_server, "irrigate_instance", app)
  api_server.invalidate_next_runs_cache()
  try:
    with TestClient(api_server.app) as client:
      for age in (60.001, 600, 660, 660.001):
        app.clock.advance(start + age - app.clock.monotonic())
        for _ in range(3):
          status, health = client.get("/api/status"), client.get("/api/health")
          assert status.status_code == health.status_code == 200
          assert status.json()["waterflow"]["flow_rate_lpm"] == (value or 0)
          assert status.json()["waterflow"]["last_update"] == (
            original["timestamp"].isoformat() if value is not None else None)
          assert status.json()["waterflow"]["history"] == history
          assert not health.json()["monitoring"]["waterflow"]["fresh"]
          assert app.alerts.get_health() == counters
      assert flow.snapshot()["received"] == original["received"]
      assert app._start_mono == start
      assert flow_notifications(channel) == []
      monitor(app)
      incident, counters = flow_incident(app), app.alerts.get_health()
      flow.setLastLiter_1m(0)
      for _ in range(3):
        client.get("/api/status")
        assert client.get("/api/health").json()["monitoring"]["waterflow"]["fresh"]
        assert flow_incident(app) is incident
        assert app.alerts.get_health() == counters
      monitor(app)
      assert flow_incident(app) is None
      assert len(flow_notifications(channel)) == 1
  finally:
    api_server.invalidate_next_runs_cache()


def test_deferred_warning_does_not_extend_measurements_accounting_or_manual_deadline(notification_app_factory):
  app, channel = notification_app_factory()
  app.waterflow.setLastLiter_1m(2)
  operation = app.controller.start_manual("Valve A", 99)
  deadline = operation.deadline
  assert deadline == app.clock.monotonic() + 1800
  for seconds in (60.001, 599.999):
    app.clock.advance(seconds)
    app.controller.tick()
    monitor(app)
    assert flow_notifications(channel) == []
    assert operation.liters == pytest.approx(2)
    assert not operation.complete
    assert not operation.no_flow
    assert operation.last_positive is None
    assert operation.deadline == deadline
  app.clock.advance(0.001)
  app.controller.tick()
  monitor(app)
  assert len(flow_notifications(channel)) == 1
  app.clock.advance(deadline - app.clock.monotonic())
  app.controller.tick()
  monitor(app)
  assert not app.valves["Valve A"].is_open
  assert operation.open_seconds == pytest.approx(1800)
  assert operation.liters == pytest.approx(2)
  assert app.metrics.get_health()["unavailable_seconds"] == pytest.approx(1740)
  assert not any(alert.type in (
    AlertType.SAFETY_INTERVENTION, AlertType.ACTUATION_FAILURE, AlertType.MALFUNCTION_NO_FLOW,
  ) for alert in channel.calls)
  assert len(flow_notifications(channel)) == 1


def test_leak_repeats_and_fresh_no_flow_notifications_are_not_suppressed(notification_app_factory):
  app, channel = notification_app_factory()
  app.clock.advance(60)
  app.waterflow.setLastLiter_1m(2)
  app._check_leak()
  dispatch(app)
  leak_key = (AlertType.LEAK, None, None)
  incident = app.alerts._incidents[leak_key]
  app.waterflow.setLastLiter_1m(0)
  app.clock.advance(61)
  monitor(app)
  app._check_leak()
  assert app.alerts._incidents[leak_key] is incident
  app.clock.advance(839)
  app.waterflow.setLastLiter_1m(2)
  app._check_leak()
  dispatch(app)
  leaks = [alert for alert in channel.calls if alert.type == AlertType.LEAK]
  assert len(leaks) == 2
  assert all(alert.severity.value == "critical" for alert in leaks)
  app.waterflow.setLastLiter_1m(0)
  app._check_leak()
  assert leak_key not in app.alerts._incidents
  operation = app.controller.start_manual("Valve A", 5)
  for _ in range(6):
    app.clock.advance(10)
    app.waterflow.setLastLiter_1m(0)
    app.controller.tick()
    monitor(app)
  assert operation.no_flow
  assert len([alert for alert in channel.calls if alert.type == AlertType.MALFUNCTION_NO_FLOW]) == 1
  assert flow_notifications(channel) == []


def test_delivery_failure_keeps_one_incident_and_the_existing_bounded_retry_budget(notification_app_factory):
  app, channel = notification_app_factory()
  channel.default = False
  app.waterflow.setLastLiter_1m(2)
  app.clock.advance(661)
  monitor(app)
  incident = flow_incident(app)
  for delay in (2, 4, 8, 16):
    app.clock.advance(delay)
    monitor(app)
  assert len(flow_notifications(channel)) == 5
  assert len({id(alert) for alert in flow_notifications(channel)}) == 1
  for _ in range(3):
    app.clock.advance(3600)
    monitor(app)
  health = app.alerts.get_health()
  assert flow_incident(app) is incident
  assert len(flow_notifications(channel)) == 5
  assert health["occurrences"] == 1
  assert health["errors"]["retry_exhausted"] == 1
  assert health["failed_incidents"] == 1
  assert health["delivered"] == 0
  assert not health["healthy"]


def test_genuine_recovery_cancels_obsolete_delivery_retries(notification_app_factory):
  app, channel = notification_app_factory()
  channel.default = False
  app.clock.advance(661)
  monitor(app)
  assert len(flow_notifications(channel)) == 1
  assert app.alerts.get_health()["queue_depth"] == 1
  app.waterflow.setLastLiter_1m(0)
  monitor(app)
  assert app.alerts.get_health()["queue_depth"] == 0
  app.clock.advance(2)
  monitor(app)
  assert len(flow_notifications(channel)) == 1
  channel.default = True
  app.clock.advance(659)
  monitor(app)
  assert len(flow_notifications(channel)) == 2
  assert app.alerts.get_health()["delivered"] == 1
