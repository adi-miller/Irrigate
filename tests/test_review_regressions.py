import csv
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytz
from fastapi.testclient import TestClient

import api_server
from controller import ControlError
from model import Job
from schedule_simulator import ScheduleSimulator
from scheduling import adjusted_duration
from tests.runtime_support import FakeClock, make_app, make_config
from tests.test_controller import controller_fixture


def submit(app, action, payload=b"", name="Valve_A"):
  app.submit_mqtt("fixturePi/%s/%s/command" % (action, name), payload)


def drain(app):
  app._drain_mqtt(stops_only=True)
  for _ in range(8):
    app._drain_mqtt()
  assert app._mqtt_commands.unfinished_tasks == 0


@pytest.mark.parametrize("actions,remaining,opens", [
  ([("forceopen", b"2"), ("forceclose", b"")], 0, 0),
  ([("forceclose", b""), ("forceopen", b"1")], 60, 1),
  ([("forceopen", b"2"), ("forceclose", b""), ("forceopen", b"1")], 60, 1),
])
def test_later_mqtt_close_cancels_only_older_immediate_opens(tmp_path, actions, remaining, opens):
  app = make_app(tmp_path, configuration=make_config(count=2))
  try:
    queued = Job(app.valves["Valve B"], 5, None)
    app.controller.enqueue(queued)
    for action, payload in actions:
      submit(app, action, payload)
    drain(app)
    valve = app.valves["Valve A"]
    assert sum(action == "open" for action, _ in valve.calls) == opens
    assert valve.secondsRemain == remaining
    assert app.controller.queue_snapshot() == [queued]
    assert valve.is_open is bool(opens)
  finally:
    app.shutdown()


@pytest.mark.parametrize("end", ["mqtt_close", "expiry"])
def test_received_repeat_open_cannot_become_a_new_operation_after_release(tmp_path, end, caplog):
  app = make_app(tmp_path)
  try:
    app.controller.start_manual("Valve A", 1)
    submit(app, "forceopen", b"30")
    if end == "mqtt_close":
      submit(app, "forceclose")
    else:
      app.clock.advance(60)
      app.controller.tick()
    drain(app)
    valve = app.valves["Valve A"]
    assert not valve.is_open and valve.name not in app.controller.operations
    assert sum(action == "open" for action, _ in valve.calls) == 1
    assert "rejected" in caplog.text.lower()
  finally:
    app.shutdown()


def test_controller_stop_cancels_pending_mqtt_open_but_not_a_subsequent_open(tmp_path):
  app = make_app(tmp_path)
  try:
    submit(app, "forceopen", b"2")
    app.controller.stop("Valve A")
    drain(app)
    assert not app.valves["Valve A"].is_open
    submit(app, "forceopen", b"1")
    drain(app)
    assert app.valves["Valve A"].secondsRemain == 60
    assert sum(action == "open" for action, _ in app.valves["Valve A"].calls) == 1
  finally:
    app.shutdown()


def test_stop_between_mqtt_dequeue_and_admission_still_cancels_old_open(tmp_path, monkeypatch):
  app = make_app(tmp_path)
  original = app.controller.start_manual

  def after_stop(name, duration=None, **kwargs):
    app.controller.stop(name)
    return original(name, duration, **kwargs)

  try:
    monkeypatch.setattr(app.controller, "start_manual", after_stop)
    submit(app, "forceopen", b"1")
    drain(app)
    assert not app.valves["Valve A"].is_open
    assert not any(action == "open" for action, _ in app.valves["Valve A"].calls)
  finally:
    app.shutdown()


def test_close_arriving_during_close_is_not_lost_or_applied_after_a_later_open(tmp_path, monkeypatch):
  app = make_app(tmp_path)
  valve = app.valves["Valve A"]
  original = valve.close
  injected = False

  def close():
    nonlocal injected
    original()
    if not injected:
      injected = True
      submit(app, "forceopen", b"2")
      submit(app, "forceclose")
      submit(app, "forceopen", b"1")

  try:
    app.controller.start_manual(valve.name, 1)
    monkeypatch.setattr(valve, "close", close)
    submit(app, "forceclose")
    drain(app)
    assert valve.is_open and valve.secondsRemain == 60
    assert sum(action == "open" for action, _ in valve.calls) == 2
    assert app._mqtt_stops == {}
  finally:
    app.shutdown()


@pytest.mark.parametrize("unavailable", [False, True])
def test_sensor_resume_cannot_open_during_another_valves_fault(unavailable):
  controller, clock, valves, sensor, _, _ = controller_fixture(count=2)
  sensor.disable = True
  controller.enqueue(Job(valves["Valve B"], 2, valves["Valve B"].schedules[0]))
  controller.tick()
  deadline = controller.operations["Valve B"].deadline
  valves["Valve A"].fail_close = 3
  with pytest.raises(ControlError):
    controller.stop("Valve A")
  assert not controller.ready
  sensor.disable = False
  sensor.exception = unavailable
  clock.advance(10)
  controller.tick()
  assert not any(action == "open" for action, _ in valves["Valve B"].calls)
  assert controller.operations["Valve B"].deadline == deadline
  controller.stop("Valve A")
  controller.tick()
  assert valves["Valve B"].is_open
  assert controller.operations["Valve B"].deadline == deadline
  clock.advance(110)
  controller.tick()
  assert not valves["Valve B"].is_open


def test_waiting_deadline_still_expires_while_global_readiness_is_faulted():
  controller, clock, valves, sensor, _, _ = controller_fixture(count=2)
  sensor.disable = True
  controller.enqueue(Job(valves["Valve B"], 1, valves["Valve B"].schedules[0]))
  controller.tick()
  valves["Valve A"].fail_close = 3
  with pytest.raises(ControlError):
    controller.stop("Valve A")
  sensor.disable = False
  clock.advance(60)
  controller.tick()
  assert "Valve B" not in controller.operations
  assert not any(action == "open" for action, _ in valves["Valve B"].calls)
  assert not controller.ready


@pytest.mark.parametrize("derived", [False, True])
def test_sub_resolution_configuration_is_rejected_before_commit(tmp_path, derived):
  app = make_app(tmp_path)
  try:
    path = Path(app.cfg.filename)
    before = path.read_bytes(), app.cfg.get_data()

    def mutate(data):
      schedule = data["valves"][0]["schedules"][0]
      if derived:
        schedule["enable_uv_adjustments"] = True
        data["sensors"][0]["uv_adjustments"][0]["multiplier"] = 1e-100
      else:
        schedule["duration"] = 1e-100

    with pytest.raises(ValueError, match="duration"):
      app.update_config(mutate)
    assert (path.read_bytes(), app.cfg.get_data()) == before
    assert not Path(app.cfg.last_good_filename).exists()
  finally:
    app.shutdown()


def test_api_sub_resolution_schedule_is_an_error_without_mutation(tmp_path, monkeypatch):
  app = make_app(tmp_path)
  try:
    monkeypatch.setattr(api_server, "irrigate_instance", app)
    before = Path(app.cfg.filename).read_bytes()
    with TestClient(api_server.app) as client:
      response = client.put("/api/valves/Valve%20A/schedules/0", json={"duration": 1e-100})
    assert response.status_code == 400
    assert Path(app.cfg.filename).read_bytes() == before
    assert app.valves["Valve A"].schedules[0].duration == 5
  finally:
    app.shutdown()


def test_adjusted_positive_duration_must_not_round_to_zero():
  schedule = SimpleNamespace(duration=5, enable_uv_adjustments=True)
  with pytest.raises(ValueError, match="represent"):
    adjusted_duration(schedule, 1e-100)
  assert adjusted_duration(schedule, 0) == 0


def test_expected_job_rejection_does_not_terminate_unrelated_watering(tmp_path, monkeypatch, caplog):
  app = make_app(tmp_path, configuration=make_config(count=2))
  try:
    active = app.controller.start_manual("Valve A", 1)
    app.valves["Valve B"].schedules[0].fixed_start_time = "10:00"
    original = app.calculateJobDuration
    monkeypatch.setattr(
      app, "calculateJobDuration",
      lambda valve, schedule: 1e-100 if valve.name == "Valve B" else original(valve, schedule),
    )
    app.maintenance_tick()
    assert app.valves["Valve A"].is_open and not app.terminated
    assert app.controller.operations["Valve A"] is active
    assert "rejected" in caplog.text.lower()
    app.clock.advance(60)
    app.controller.tick()
    assert not app.valves["Valve A"].is_open
    assert not any(action == "open" for action, _ in app.valves["Valve B"].calls)
  finally:
    app.shutdown()


def csv_rows(app):
  path = Path(app.metrics.metrics_file)
  if not path.exists():
    return []
  with path.open(newline="", encoding="utf-8") as source:
    return list(csv.DictReader(source))


def test_midnight_maintenance_cannot_export_before_the_last_interval(tmp_path, monkeypatch):
  clock = FakeClock(datetime(2026, 9, 18, 23, 59, 59, tzinfo=timezone.utc))
  app = make_app(tmp_path, clock)
  original_flush = app.metrics.flush

  def outside_lock():
    assert not app.controller.lock._is_owned()
    return original_flush()

  monkeypatch.setattr(app.metrics, "flush", outside_lock)
  try:
    app.waterflow.setLastLiter_1m(6)
    app.controller.start_manual("Valve A", 1)
    clock.advance(0.9)
    app.controller.tick()
    clock.advance(0.2)
    app.maintenance_tick()
    app.controller.tick()
    assert app.metrics.flush()
    rows = csv_rows(app)
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-09-18"
    assert float(rows[0]["total_seconds"]) == pytest.approx(1)
    assert float(rows[0]["total_liters"]) == pytest.approx(0.1)
    assert app.metrics.daily_totals("2026-09-19")["Valve A"]["seconds"] == pytest.approx(0.1)
    assert app.metrics.get_health()["late_adjustment_seconds"] == 0
    assert not app.metrics.get_health()["export_limited"]
    assert app.metrics.get_quality()["2026-09-18"]["Valve A"]
  finally:
    app.shutdown()


def test_daytime_restart_exports_recovered_prior_days_once_before_baselines(tmp_path):
  clock = FakeClock(datetime(2026, 9, 18, 12, tzinfo=timezone.utc))
  app = make_app(tmp_path, clock)
  try:
    app.waterflow.setLastLiter_1m(6)
    app.controller.start_manual("Valve A", 1)
    clock.advance(10)
    app.controller.stop("Valve A")
  finally:
    app.shutdown()
  assert csv_rows(app) == []
  for day in (19, 19, 20):
    app = make_app(tmp_path, FakeClock(datetime(2026, 9, day, 12, tzinfo=timezone.utc)))
    try:
      rows = csv_rows(app)
      assert [(row["date"], float(row["total_seconds"])) for row in rows] == [("2026-09-18", 10)]
      assert app.valves["Valve A"].baseline_sample_count == 1
      assert app.controller.operations == {}
      assert app.valves["Valve A"].calls == [("close", app.clock.monotonic())]
      assert app.metrics.flush()
      assert len(csv_rows(app)) == 1
    finally:
      app.shutdown()


def test_idle_mqtt_enablement_publishes_even_without_periodic_telemetry(tmp_path, monkeypatch):
  configuration = make_config()
  configuration["telemetry"]["enabled"] = False
  app = make_app(tmp_path, configuration=configuration)
  messages = []

  def publish(topic, payload):
    assert not app.controller.lock._is_owned()
    messages.append((topic, payload))
    return True

  monkeypatch.setattr(app.mqtt, "publish", publish)
  try:
    app.controller.drain_events()
    for payload, status in ((b"0", "disabled"), (b"1", "enabled")):
      messages.clear()
      submit(app, "enabled", payload)
      drain(app)
      app.maintenance_tick()
      assert ("Valve A/status", status) in messages
    assert [action for action, _ in app.valves["Valve A"].calls] == ["close"]
  finally:
    app.shutdown()


def test_fallback_multiday_simulation_and_next_runs_use_local_calendar_dates(tmp_path, monkeypatch):
  zone = pytz.timezone("America/New_York")
  cfg = make_config(sensor=False)
  cfg["timezone"] = zone.zone
  cfg["location"] = {"latitude": 40.7, "longitude": -74.0}
  cfg["valves"][0]["schedules"][0].update(days=["Mon"], fixed_start_time="06:00")
  clock = FakeClock(zone.localize(datetime(2026, 10, 31)))
  app = make_app(tmp_path, clock, configuration=cfg)
  try:
    simulator = ScheduleSimulator(app)
    simulator.parse_schedule_options("date:2026-10-31,time:00:00,days:3")
    jobs = simulator.get_scheduled_jobs_for_simulation()
    assert len(jobs) == 1
    assert jobs[0]["schedule_time"].isoformat() == "2026-11-02T06:00:00-05:00"
    assert jobs[0]["sim_date"].isoformat() == "2026-11-02"
    monkeypatch.setattr(api_server, "irrigate_instance", app)
    api_server.invalidate_next_runs_cache()
    with TestClient(api_server.app) as client:
      result = client.get("/api/next-runs").json()["next_runs"]["Valve A"]
    assert result["schedule_time_iso"] == "2026-11-02T06:00:00-05:00"
  finally:
    app.shutdown()
    api_server.invalidate_next_runs_cache()
