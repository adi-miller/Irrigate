import copy
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server
from alerts import AlertType
from model import Job
from sensors.base_sensor import SensorUnavailable
from tests.runtime_support import FakeClock, make_app, make_config
from tests.test_contracts import CASES


@pytest.fixture
def runtime_factory(tmp_path, monkeypatch):
    instances = []

    def build(configuration=None, clock=None, **kwargs):
        directory = tmp_path / ("app-%s" % len(instances))
        directory.mkdir()
        instance = make_app(directory, configuration=configuration, clock=clock, **kwargs)
        instances.append(instance)
        monkeypatch.setattr(api_server, "irrigate_instance", instance)
        api_server.invalidate_next_runs_cache()
        return instance

    yield build
    for instance in reversed(instances):
        instance.shutdown("API test complete")
    api_server.invalidate_next_runs_cache()


@pytest.fixture
def runtime(runtime_factory):
    return runtime_factory()


@pytest.fixture
def client(runtime):
    with TestClient(api_server.app, raise_server_exceptions=False) as transport:
        yield transport


def disk_state(instance):
    path = Path(instance.cfg.filename)
    backup = Path(instance.cfg.last_good_filename)
    return (
        path.read_bytes(), backup.read_bytes() if backup.exists() else None,
        instance.cfg.get_data(),
    )


def calls(instance):
    return {name: list(valve.calls) for name, valve in instance.valves.items()}


def forbidden(*args, **kwargs):
    raise AssertionError("Read-only API attempted a side effect or consumed another observation")


@pytest.mark.parametrize(("name", "method", "url", "body"), CASES + [
    ("health", "GET", "/api/health", None),
])
def test_all_api_routes_reject_missing_initialization(monkeypatch, name, method, url, body):
    monkeypatch.setattr(api_server, "irrigate_instance", None)
    with TestClient(api_server.app, raise_server_exceptions=False) as client:
        response = client.request(method, url, json=body) if body is not None else client.request(method, url)
    assert response.status_code == 503, (name, response.text)
    assert response.json() == {"detail": "System not initialized"}


@pytest.mark.parametrize(("method", "url", "body"), [
    ("GET", "/api/valves/Unknown", None),
    ("POST", "/api/valves/Unknown/start-manual", None),
    ("POST", "/api/valves/Unknown/queue?duration_minutes=2", None),
    ("POST", "/api/valves/Unknown/stop", None),
    ("POST", "/api/valves/Unknown/enable", None),
    ("POST", "/api/valves/Unknown/disable", None),
    ("PUT", "/api/valves/Unknown/enabled?enabled=false", None),
    ("POST", "/api/valves/Unknown/schedules", {"fixed_start_time": "12:00"}),
    ("PUT", "/api/valves/Unknown/schedules/0", {"duration": 10}),
    ("DELETE", "/api/valves/Unknown/schedules/0", None),
])
def test_unknown_valve_is_404_without_mutation(client, runtime, method, url, body):
    before, pulses = disk_state(runtime), calls(runtime)
    response = client.request(method, url, json=body) if body is not None else client.request(method, url)
    assert response.status_code == 404
    assert disk_state(runtime) == before
    assert calls(runtime) == pulses


def test_query_signatures_and_required_queue_duration(client, runtime, caplog):
    schema = api_server.app.openapi()["paths"]
    manual = schema["/api/valves/{valve_name}/start-manual"]["post"]["parameters"]
    manual = next(item for item in manual if item["name"] == "duration_minutes")
    assert manual["in"] == "query" and manual["required"] is False
    assert manual["schema"]["default"] == 30
    queue = schema["/api/valves/{valve_name}/queue"]["post"]["parameters"]
    assert next(item for item in queue if item["name"] == "duration_minutes")["required"] is True
    enabled = schema["/api/valves/{valve_name}/enabled"]["put"]
    assert "requestBody" not in enabled
    assert next(item for item in enabled["parameters"] if item["name"] == "enabled")["in"] == "query"
    pulses = calls(runtime)
    with caplog.at_level(logging.WARNING):
        response = client.post("/api/valves/Valve%20A/queue")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "duration_minutes"]
    assert "validation rejected" in caplog.text
    assert calls(runtime) == pulses
    assert runtime.controller.queue_snapshot() == []


@pytest.mark.parametrize("action", ["start-manual", "queue"])
@pytest.mark.parametrize(("value", "status"), [
    ("NaN", 400), ("inf", 400), ("-inf", 400), ("1e309", 400),
    ("0", 400), ("-0.1", 400), ("malformed-secret\npayload", 422), ("true", 422),
])
def test_invalid_control_input_is_logged_without_actuation(
    client, runtime, caplog, action, value, status,
):
    before, pulses = disk_state(runtime), calls(runtime)
    with caplog.at_level(logging.INFO):
        response = client.post(
            "/api/valves/Valve%20A/" + action, params={"duration_minutes": value},
        )
    assert response.status_code == status, response.text
    assert "success" not in response.json()
    api_logs = "\n".join(record.getMessage() for record in caplog.records if record.name == runtime.logger.name)
    assert "reject" in api_logs.lower()
    assert "malformed-secret" not in api_logs
    assert "payload" not in api_logs
    if status == 422:
        assert isinstance(response.json()["detail"], list)
        assert response.json()["detail"][0]["loc"] == ["query", "duration_minutes"]
    assert runtime.controller.operations == {}
    assert runtime.controller.queue_snapshot() == []
    assert disk_state(runtime) == before
    assert calls(runtime) == pulses


@pytest.mark.parametrize("duration", [None, 1.25, 75.5])
def test_manual_default_and_explicit_cap_preserve_legacy_response(client, runtime, duration):
    parameters = {} if duration is None else {"duration_minutes": duration}
    response = client.post("/api/valves/Valve%20A/start-manual", params=parameters)
    assert response.status_code == 200
    assert response.json() == {"success": True, "valve": "Valve A", "action": "opened_manual"}
    operation = runtime.controller.operations["Valve A"]
    expected = 30 if duration is None else min(duration, 30)
    assert operation.duration_seconds == expected * 60
    assert operation.deadline == runtime.clock.monotonic() + expected * 60
    assert runtime.valves["Valve A"].is_open is True


def test_queue_duration_is_not_manually_capped(client, runtime, caplog):
    pulses = calls(runtime)
    response = client.post("/api/valves/Valve%20A/queue?duration_minutes=75.5")
    assert response.status_code == 200
    assert response.json() == {
        "success": True, "valve": "Valve A", "duration_minutes": 75.5,
        "action": "queued", "queued_at": "2025-06-15T10:00:00",
    }
    assert runtime.controller.queue_snapshot()[0].duration == 75.5
    assert calls(runtime) == pulses
    with caplog.at_level(logging.INFO):
        invalid = client.post("/api/valves/Valve%20A/queue?duration_minutes=1e300")
    assert invalid.status_code == 400
    assert len(runtime.controller.queue_snapshot()) == 1
    assert "reject" in caplog.text.lower()
    assert calls(runtime) == pulses


def test_repeat_manual_conflict_does_not_extend_or_reopen(client, runtime):
    assert client.post("/api/valves/Valve%20A/start-manual?duration_minutes=1").status_code == 200
    operation = runtime.controller.operations["Valve A"]
    deadline, pulses = operation.deadline, calls(runtime)
    runtime.clock.advance(10)
    response = client.post("/api/valves/Valve%20A/start-manual?duration_minutes=20")
    assert response.status_code == 409
    assert runtime.controller.operations["Valve A"] is operation
    assert operation.deadline == deadline
    assert calls(runtime) == pulses
    assert client.post("/api/valves/Valve%20A/stop").json() == {
        "success": True, "valve": "Valve A", "action": "closed",
    }


def test_paused_conflict_and_stop_cancel_the_owned_operation(client, runtime):
    valve, sensor = runtime.valves["Valve A"], runtime.sensors["Weather"]
    sensor.disable = True
    runtime.queueJob(Job(valve, 5, valve.schedules[0]))
    runtime.controller.tick()
    operation = runtime.controller.operations[valve.name]
    assert operation.paused and not valve.is_open
    pulses = calls(runtime)
    assert client.post("/api/valves/Valve%20A/start-manual").status_code == 409
    assert calls(runtime) == pulses
    assert client.post("/api/valves/Valve%20A/stop").status_code == 200
    assert operation.cancelled
    assert valve.name not in runtime.controller.operations
    sensor.disable = False
    closed = calls(runtime)
    runtime.clock.advance(10)
    runtime.controller.tick()
    assert calls(runtime) == closed
    assert not valve.is_open
    assert runtime.q.unfinished_tasks == 0


def test_failed_close_is_503_and_health_retains_possibly_open(client, runtime, monkeypatch):
    assert client.post("/api/valves/Valve%20A/start-manual").status_code == 200
    valve = runtime.valves["Valve A"]
    attempts = []

    def failed_close():
        attempts.append("close")
        raise OSError("injected driver failure")

    with monkeypatch.context() as patch:
        patch.setattr(valve, "close", failed_close)
        response = client.post("/api/valves/Valve%20A/stop")
        assert response.status_code == 503
        assert "success" not in response.json()
        assert attempts == ["close"] * 3
        health = client.get("/api/health").json()
        assert health["ready"] is False
        assert health["valves"][0]["possibly_open"] is True
        assert health["valves"][0]["fault"]
        assert valve.is_open is True
        assert client.post("/api/valves/Valve%20A/start-manual").status_code == 503
    assert client.post("/api/valves/Valve%20A/stop").status_code == 200


def test_failed_open_is_not_reported_as_success(client, runtime, monkeypatch):
    valve = runtime.valves["Valve A"]
    with monkeypatch.context() as patch:
        patch.setattr(valve, "open", lambda: (_ for _ in ()).throw(OSError("injected open failure")))
        response = client.post("/api/valves/Valve%20A/start-manual")
        assert response.status_code == 503
        assert "success" not in response.json()
        assert client.get("/api/health").json()["ready"] is False
    assert client.post("/api/valves/Valve%20A/stop").status_code == 200


def test_startup_not_ready_rejects_controls_without_pulses(client, runtime, monkeypatch):
    monkeypatch.setattr(runtime.controller, "_startup_complete", False)
    pulses = calls(runtime)
    for path in ("start-manual", "queue?duration_minutes=2"):
        assert client.post("/api/valves/Valve%20A/" + path).status_code == 503
    assert calls(runtime) == pulses


def test_queue_get_is_concurrent_and_never_dequeues_or_reenqueues(client, runtime, monkeypatch):
    for duration in (1.25, 2.5, 3.75):
        runtime.queueJob(Job(runtime.valves["Valve A"], duration, None))
    jobs = runtime.controller.queue_snapshot()
    unfinished, pulses = runtime.q.unfinished_tasks, calls(runtime)
    with monkeypatch.context() as patch:
        patch.setattr(runtime.q, "get_nowait", forbidden)
        patch.setattr(runtime.q, "put", forbidden)
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(lambda _: client.get("/api/queue"), range(24)))
        assert all(response.status_code == 200 for response in responses)
        assert all(response.json()["queue_size"] == 3 for response in responses)
        assert all(
            [job["duration_minutes"] for job in response.json()["jobs"]] == [1.25, 2.5, 3.75]
            for response in responses
        )
    assert runtime.controller.queue_snapshot() == jobs
    assert runtime.q.unfinished_tasks == unfinished
    assert calls(runtime) == pulses


def test_weather_reads_use_one_complete_snapshot_and_no_side_effects(client, runtime, monkeypatch):
    sensor = runtime.sensors["Weather"]
    sensor.uv, sensor.recentPrecip = 0, 0
    runtime._sensor_cursors = {"Weather": 77}
    reads = []

    def snapshot():
        reads.append("snapshot")
        return {"uv": 8.0, "recentPrecip": 2.0, "received": 1000, "timestamp": runtime.clock.now()}

    before, pulses = disk_state(runtime), calls(runtime)
    with monkeypatch.context() as patch:
        patch.setattr(sensor, "snapshot", snapshot, raising=False)
        for method in ("getFactor", "shouldDisable", "getTelemetry", "getUv"):
            patch.setattr(sensor, method, forbidden)
        patch.setattr(runtime.mqtt, "publish", forbidden)
        patch.setattr(runtime.alerts, "alert", forbidden)
        for path in ("/api/status", "/api/sensors"):
            response = client.get(path)
            assert response.status_code == 200, response.text
            item = response.json()["sensors"][0]
            assert item["should_disable"] is True
            assert item["factor"] == 2.0
            assert item["telemetry"] == {"uv": 8.0, "recentPrecip": 2.0}
            assert set(item) == {"name", "type", "enabled", "should_disable", "factor", "telemetry"}
    assert reads == ["snapshot", "snapshot"]
    assert runtime._sensor_cursors == {"Weather": 77}
    assert (sensor.uv, sensor.recentPrecip) == (0, 0)
    assert disk_state(runtime) == before
    assert calls(runtime) == pulses


@pytest.mark.parametrize("failure", ["unavailable", "partial", "nonfinite"])
def test_unavailable_weather_keeps_legacy_error_shapes(client, runtime, monkeypatch, failure):
    sensor = runtime.sensors["Weather"]

    def snapshot():
        if failure == "unavailable":
            raise SensorUnavailable("missing observation")
        if failure == "partial":
            return {"uv": 2.5}
        return {"uv": float("nan"), "recentPrecip": 0.0}

    with monkeypatch.context() as patch:
        patch.setattr(sensor, "snapshot", snapshot, raising=False)
        patch.setattr(runtime.alerts, "alert", forbidden)
        full = client.get("/api/status")
        sensors = client.get("/api/sensors")
    assert full.status_code == sensors.status_code == 200
    metadata = {"name": "Weather", "type": "OpenWeatherMap", "enabled": True, "error": True}
    assert sensors.json()["sensors"] == [metadata]
    assert full.json()["sensors"] == [{
        **metadata, "should_disable": None, "factor": None, "telemetry": {},
    }]


def test_stale_flow_preserves_real_last_value_timestamp_and_history(client, runtime, monkeypatch):
    runtime.waterflow.setLastLiter_1m(2.345)
    before = runtime.waterflow.snapshot()
    history = copy.deepcopy(runtime.waterflow.getHistory())
    runtime.clock.advance(61)
    with monkeypatch.context() as patch:
        patch.setattr(runtime.waterflow, "lastLiter_1m", forbidden)
        patch.setattr(runtime.alerts, "alert", forbidden)
        first = client.get("/api/status")
        second = client.get("/api/status")
        health = client.get("/api/health")
    assert first.status_code == second.status_code == health.status_code == 200
    flow = first.json()["waterflow"]
    assert flow == second.json()["waterflow"]
    assert flow["flow_rate_lpm"] == 2.35
    assert flow["is_active"] is True
    assert flow["last_update"] == before["timestamp"].isoformat()
    assert flow["history"] == history == runtime.waterflow.getHistory()
    assert health.json()["monitoring"]["waterflow"]["available"] is False
    assert health.json()["monitoring"]["waterflow"]["fresh"] is False


def test_never_observed_flow_keeps_numeric_placeholder_but_not_a_fake_reading(client, runtime):
    flow = client.get("/api/status").json()["waterflow"]
    health = client.get("/api/health").json()["monitoring"]["waterflow"]
    assert type(flow["flow_rate_lpm"]) is int
    assert flow["flow_rate_lpm"] == 0
    assert flow["last_update"] is None and flow["history"] == []
    assert flow["is_active"] is False
    assert health["available"] is False and health["fresh"] is False
    assert runtime.waterflow.snapshot()["value"] is None


def test_no_flow_configuration_retains_legacy_disabled_projection(runtime_factory):
    configuration = make_config(sensor=False)
    configuration.pop("waterflow")
    runtime_factory(configuration=configuration)
    with TestClient(api_server.app) as client:
        response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["waterflow"] == {
        "enabled": False, "type": None, "flow_rate_lpm": 0, "is_active": False,
        "leak_detection_enabled": False, "last_update": None, "history": [],
    }


def test_health_is_separate_from_legacy_status_and_details(client, runtime):
    assert client.post("/api/valves/Valve%20A/start-manual").status_code == 200
    assert client.get("/api/health").json() == runtime.get_health()
    status = client.get("/api/status").json()
    details = client.get("/api/valves/Valve%20A").json()
    assert set(status) == {"system", "valves", "sensors", "waterflow"}
    assert set(status["valves"][0]) == {
        "name", "enabled", "is_open", "handled", "seconds_daily", "liters_daily",
        "seconds_remain", "seconds_duration", "seconds_last", "liters_last",
    }
    for data in (status["valves"][0], details):
        assert not {"operation", "deadline", "identifier", "physical_state", "ready", "fault"} & set(data)


def test_status_uses_configured_timezone_and_monotonic_uptime(runtime_factory):
    configuration = make_config()
    configuration["timezone"] = "Asia/Jerusalem"
    clock = FakeClock()
    instance = runtime_factory(configuration=configuration, clock=clock)
    started = instance.startTime.isoformat()
    clock.advance(300, wall_seconds=-3600)
    with TestClient(api_server.app) as client:
        system = client.get("/api/status").json()["system"]
    assert system["uptime_minutes"] == 5
    assert system["started_at"] == started
    assert datetime.fromisoformat(system["started_at"]).tzinfo is None
    assert system["current_time"] == "2025-06-15T12:00:00+03:00"
    for key in ("current_time", "sunrise", "sunset"):
        assert datetime.fromisoformat(system[key]).tzinfo is not None
    assert system["timezone"] == "Asia/Jerusalem"


def test_predictions_are_pure_and_cache_age_is_monotonic(client, runtime):
    before, pulses = disk_state(runtime), calls(runtime)
    valve, sensor = runtime.valves["Valve A"], runtime.sensors["Weather"]
    schedules = copy.deepcopy([vars(schedule) for schedule in valve.schedules])
    weather = sensor.uv, sensor.disable, sensor.precip_days, sensor.precip_threshold
    runtime._sensor_cursors = {"Weather": 42}
    response = client.get("/api/next-runs")
    assert response.status_code == 200
    first = response.json()
    assert set(first["next_runs"]["Valve A"]) == {
        "schedule_time", "schedule_time_iso", "duration_minutes", "schedule_index",
    }
    runtime.clock.advance(30, wall_seconds=-3600)
    second = client.get("/api/next-runs").json()
    assert second["next_runs"] == first["next_runs"]
    assert second["cache_age_seconds"] == 30
    simulated = client.post("/api/simulate?date=2025-06-15&uv=8&rain=true&days=2")
    assert simulated.status_code == 200, simulated.text
    assert runtime._sensor_cursors == {"Weather": 42}
    assert weather == (sensor.uv, sensor.disable, sensor.precip_days, sensor.precip_threshold)
    assert [vars(schedule) for schedule in valve.schedules] == schedules
    assert disk_state(runtime) == before
    assert calls(runtime) == pulses


_CONFIG_REQUESTS = [
    ("POST", "/api/config/alerts/enabled", {"alert_type": "leak", "enabled": False}),
    ("POST", "/api/config/alerts/settings", {"setting": "leak_repeat_minutes", "value": 20}),
    ("POST", "/api/config/waterflow", {"setting": "enabled", "value": False}),
    ("POST", "/api/config/sensors/Weather", {"setting": "precip_days", "value": 3}),
    ("POST", "/api/valves/Valve%20A/enable", None),
    ("POST", "/api/valves/Valve%20A/disable", None),
    ("PUT", "/api/valves/Valve%20A/enabled?enabled=false", None),
    ("PUT", "/api/valves/Valve%20A/schedules/0", {"duration": 45.5}),
    ("POST", "/api/valves/Valve%20A/schedules", {"fixed_start_time": "12:15"}),
    ("DELETE", "/api/valves/Valve%20A/schedules/1", None),
]


@pytest.mark.parametrize(("method", "url", "body"), _CONFIG_REQUESTS)
def test_transaction_failure_preserves_disk_runtime_and_cache(
    runtime_factory, monkeypatch, method, url, body,
):
    configuration = make_config()
    configuration["valves"][0]["schedules"].append({
        "time_based_on": "fixed", "fixed_start_time": "12:15", "duration": 10,
    })
    instance = runtime_factory(configuration=configuration)
    before, pulses = disk_state(instance), calls(instance)
    namespace = instance.cfg.cfg
    sensor_config = instance.sensors["Weather"].config
    flow_config = instance.waterflow.config
    attempted = []

    def fail(*args, **kwargs):
        attempted.append("persist")
        raise OSError("injected persistence failure")

    with TestClient(api_server.app, raise_server_exceptions=False) as client:
        assert client.get("/api/next-runs").status_code == 200
        cache = copy.deepcopy(api_server.next_runs_cache)
        monkeypatch.setattr(instance.cfg, "_persist", fail)
        response = client.request(method, url, json=body) if body is not None else client.request(method, url)
    assert response.status_code == 503, response.text
    assert attempted == ["persist"]
    assert "success" not in response.json()
    assert instance.cfg.cfg is namespace
    assert instance.sensors["Weather"].config is sensor_config
    assert instance.waterflow.config is flow_config
    assert disk_state(instance) == before
    assert calls(instance) == pulses
    assert api_server.next_runs_cache == cache


def test_config_file_io_is_off_event_loop_and_outside_actuator_lock(client, runtime, monkeypatch):
    event_threads, persistence_threads = [], []
    offload = api_server.run_in_threadpool
    persist = runtime.cfg._persist

    async def checked_offload(action, *args, **kwargs):
        event_threads.append(threading.get_ident())
        return await offload(action, *args, **kwargs)

    def checked_persist(*args, **kwargs):
        persistence_threads.append(threading.get_ident())
        assert threading.get_ident() not in event_threads
        assert not runtime.controller.lock._is_owned()
        assert runtime.controller.lock.acquire(blocking=False)
        runtime.controller.lock.release()
        return persist(*args, **kwargs)

    monkeypatch.setattr(api_server, "run_in_threadpool", checked_offload)
    monkeypatch.setattr(runtime.cfg, "_persist", checked_persist)
    response = client.put("/api/valves/Valve%20A/schedules/0", json={"duration": 45.5})
    assert response.status_code == 200, response.text
    assert len(event_threads) == len(persistence_threads) == 1
    assert runtime.valves["Valve A"].schedules[0].duration == 45.5


def test_successful_commit_reloads_alerts_flow_and_sensor_settings(client, runtime):
    assert client.post("/api/config/alerts/enabled", json={
        "alert_type": "actuation_failure", "enabled": False,
    }).status_code == 200
    assert runtime.alerts.enabled[AlertType.ACTUATION_FAILURE] is False
    assert client.post("/api/config/waterflow", json={"setting": "enabled", "value": False}).status_code == 200
    assert runtime.waterflow.enabled is False
    assert runtime.waterflow.get_health()["available"] is False
    assert client.post("/api/config/waterflow", json={
        "setting": "leak_detection", "value": False,
    }).status_code == 200
    assert runtime.waterflow.leakdetection is False
    assert client.post("/api/config/sensors/Weather", json={
        "setting": "precip_days", "value": "3",
    }).status_code == 200
    assert runtime.sensors["Weather"].precip_days == 3
    assert json.loads(Path(runtime.cfg.filename).read_text(encoding="utf-8")) == runtime.cfg.get_data()


@pytest.mark.parametrize(("url", "setting", "value", "stored"), [
    ("/api/config/alerts/settings", "leak_repeat_minutes", "20", 20),
    ("/api/config/alerts/settings", "leak_repeat_minutes", 20.75, 20),
    ("/api/config/alerts/settings", "irregular_flow_threshold", "2.75", 2.75),
    ("/api/config/sensors/Weather", "precip_days", "3", 3),
    ("/api/config/sensors/Weather", "precip_threshold", "2.75", 2.75),
])
def test_numeric_setting_conversion_preserves_original_success_echo(
    client, runtime, url, setting, value, stored,
):
    response = client.post(url, json={"setting": setting, "value": value})
    assert response.status_code == 200, response.text
    expected = {"success": True, "setting": setting, "value": value}
    if "/sensors/" in url:
        expected["sensor"] = "Weather"
        assert getattr(runtime.sensors["Weather"], setting) == stored
    else:
        assert runtime.cfg.get_data()["alerts"][setting] == stored
    assert response.json() == expected


@pytest.mark.parametrize(("url", "body"), [
    ("/api/config/alerts/enabled", {"alert_type": "leak", "enabled": "false"}),
    ("/api/config/alerts/enabled", {"alert_type": "leak", "enabled": 1}),
    ("/api/config/alerts/enabled", {"alert_type": "absent", "enabled": False}),
    ("/api/config/waterflow", {"setting": "enabled", "value": "false"}),
    ("/api/config/waterflow", {"setting": "leak_detection", "value": 0}),
    ("/api/config/alerts/settings", {"setting": "leak_repeat_minutes", "value": -2}),
    ("/api/config/alerts/settings", {"setting": "irregular_flow_threshold", "value": "NaN"}),
    ("/api/config/alerts/settings", {"setting": "irregular_flow_threshold", "value": "inf"}),
    ("/api/config/sensors/Weather", {"setting": "precip_days", "value": True}),
    ("/api/config/sensors/Weather", {"setting": "precip_days", "value": -1}),
    ("/api/config/sensors/Weather", {"setting": "precip_threshold", "value": "invalid"}),
])
def test_invalid_settings_never_mutate_configuration(client, runtime, url, body):
    before, pulses = disk_state(runtime), calls(runtime)
    response = client.post(url, json=body)
    assert response.status_code == 400, response.text
    assert disk_state(runtime) == before
    assert calls(runtime) == pulses


@pytest.mark.parametrize("patch_data", [
    {"duration": 0}, {"duration": -1}, {"duration": True}, {"duration": float("nan")},
    {"duration": float("inf")}, {"duration": 1e300}, {"fixed_start_time": "24:00"},
    {"duration": 1e12, "enable_uv_adjustments": True},
    {"days": ["Sunday"]}, {"enable_uv_adjustments": "true"}, {"unknown": "discarding is not validation"},
])
def test_invalid_schedule_edits_leave_source_and_runtime_untouched(client, runtime, patch_data):
    before, pulses = disk_state(runtime), calls(runtime)
    schedules = runtime.valves["Valve A"].schedules
    response = client.put(
        "/api/valves/Valve%20A/schedules/0",
        content=json.dumps(patch_data), headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400, response.text
    assert disk_state(runtime) == before
    assert runtime.valves["Valve A"].schedules is schedules
    assert calls(runtime) == pulses


def test_schedule_crud_defaults_partial_updates_and_last_schedule_rejection(client, runtime):
    assert client.get("/api/next-runs").status_code == 200
    assert api_server.next_runs_cache["data"] is not None
    created = client.post("/api/valves/Valve%20A/schedules", json={"fixed_start_time": "6:15"})
    assert created.status_code == 200, created.text
    assert created.json() == {
        "success": True, "valve": "Valve A", "schedule_index": 1, "action": "schedule_created",
    }
    assert api_server.next_runs_cache["data"] is None
    schedule = runtime.cfg.get_data()["valves"][0]["schedules"][1]
    assert schedule == {
        "fixed_start_time": "6:15", "time_based_on": "fixed", "duration": 10,
        "days": [], "seasons": [], "enable_uv_adjustments": False,
    }
    assert client.put("/api/valves/Valve%20A/schedules/1", json={"duration": 45.5}).status_code == 200
    expected = dict(schedule, duration=45.5)
    assert runtime.cfg.get_data()["valves"][0]["schedules"][1] == expected
    solar = client.post("/api/valves/Valve%20A/schedules", json={"time_based_on": "sunrise"})
    assert solar.status_code == 200
    assert runtime.valves["Valve A"].schedules[2].offset_minutes == 0
    assert client.delete("/api/valves/Valve%20A/schedules/2").json()["remaining_schedules"] == 2
    assert client.delete("/api/valves/Valve%20A/schedules/1").json()["remaining_schedules"] == 1
    before = disk_state(runtime)
    assert client.delete("/api/valves/Valve%20A/schedules/0").status_code == 400
    assert client.delete("/api/valves/Valve%20A/schedules/9").status_code == 404
    assert client.put("/api/valves/Valve%20A/schedules/-1", json={"duration": 10}).status_code == 404
    assert disk_state(runtime) == before


def test_zero_uv_multiplier_suppresses_predictions_without_rejecting_config(runtime_factory):
    configuration = make_config()
    configuration["sensors"][0]["uv_adjustments"] = [{"max_uv_index": 10, "multiplier": 0}]
    configuration["valves"][0]["schedules"][0].update(duration=1e12, enable_uv_adjustments=True)
    instance = runtime_factory(configuration=configuration)
    pulses = calls(instance)
    with TestClient(api_server.app, raise_server_exceptions=False) as client:
        prediction = client.get("/api/next-runs")
        assert prediction.status_code == 200, prediction.text
        assert prediction.json()["next_runs"] == {}
        response = client.put("/api/valves/Valve%20A/schedules/0", json={"duration": 45.5})
        assert response.status_code == 200, response.text
        assert client.get("/api/next-runs").json()["next_runs"] == {}
    assert instance.valves["Valve A"].schedules[0].duration == 45.5
    assert instance.controller.operations == {}
    assert calls(instance) == pulses


def test_put_enabled_remains_query_only_and_overrides_runtime_mqtt_enable(client, runtime):
    assert client.put("/api/valves/Valve%20A/enabled", json={"enabled": False}).status_code == 422
    runtime.controller.set_enabled("Valve A", False)
    assert runtime.cfg.get_data()["valves"][0]["enabled"] is True
    assert client.post("/api/config/alerts/settings", json={
        "setting": "leak_repeat_minutes", "value": 20,
    }).status_code == 200
    assert runtime.valves["Valve A"].enabled is False
    response = client.put("/api/valves/Valve%20A/enabled?enabled=true")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "success": True, "valve": "Valve A", "enabled": True, "action": "enabled_updated",
    }
    assert runtime.valves["Valve A"].enabled is True
    assert runtime.cfg.get_data()["valves"][0]["enabled"] is True


def test_disabling_a_queued_operation_closes_after_commit(client, runtime, monkeypatch):
    valve = runtime.valves["Valve A"]
    runtime.queueJob(Job(valve, 5, None))
    runtime.controller.tick()
    assert valve.is_open
    close = valve.close
    observations = []

    def checked_close():
        observations.append(json.loads(Path(runtime.cfg.filename).read_text(encoding="utf-8")))
        return close()

    monkeypatch.setattr(valve, "close", checked_close)
    response = client.post("/api/valves/Valve%20A/disable")
    assert response.status_code == 200, response.text
    assert observations and observations[0]["valves"][0]["enabled"] is False
    assert not valve.enabled and not valve.is_open
    assert valve.name not in runtime.controller.operations


def test_disabling_cannot_report_success_when_the_close_fails(client, runtime, monkeypatch):
    valve = runtime.valves["Valve A"]
    runtime.queueJob(Job(valve, 5, None))
    runtime.controller.tick()
    with monkeypatch.context() as patch:
        patch.setattr(valve, "close", lambda: (_ for _ in ()).throw(OSError("close failed")))
        response = client.post("/api/valves/Valve%20A/disable")
        assert response.status_code == 503
        assert "success" not in response.json()
        assert valve.is_open
        assert not valve.enabled
        assert client.get("/api/health").json()["ready"] is False
    assert client.post("/api/valves/Valve%20A/stop").status_code == 200


@pytest.mark.parametrize(("method", "url", "body"), [
    ("PUT", "/api/valves/Valve%20A/schedules/0", {"duration": 45.5}),
    ("POST", "/api/config/alerts/settings", {"setting": "leak_repeat_minutes", "value": 20}),
    ("POST", "/api/config/sensors/Weather", {"setting": "precip_days", "value": 3}),
])
def test_unrelated_save_keeps_mqtt_disable_and_cannot_open_queued_work(
    client, runtime, monkeypatch, method, url, body,
):
    assert runtime.mqtt.processMessages("fixturePi/enabled/Valve_A/command", b"0")
    assert runtime.cfg.get_data()["valves"][0]["enabled"] is True
    valve = runtime.valves["Valve A"]
    runtime.queueJob(Job(valve, 5, None))
    pulses, options = calls(runtime), []
    update_config = runtime.update_config

    def checked_update(mutator, **kwargs):
        options.append(kwargs)
        return update_config(mutator, **kwargs)

    monkeypatch.setattr(runtime, "update_config", checked_update)
    response = client.request(method, url, json=body)
    assert response.status_code == 200, response.text
    assert options == [{}]
    assert valve.enabled is False
    assert runtime.controller._runtime_enabled == {"Valve A": False}
    assert runtime.controller.queue_snapshot() == []
    assert runtime.controller.operations == {}
    assert runtime.cfg.get_data()["valves"][0]["enabled"] is True
    assert json.loads(Path(runtime.cfg.filename).read_text(encoding="utf-8"))["valves"][0]["enabled"] is True
    assert calls(runtime) == pulses


@pytest.mark.parametrize(("method", "url", "desired"), [
    ("POST", "/api/valves/Valve%20A/enable", True),
    ("POST", "/api/valves/Valve%20A/disable", False),
    ("PUT", "/api/valves/Valve%20A/enabled?enabled=true", True),
    ("PUT", "/api/valves/Valve%20A/enabled?enabled=false", False),
])
@pytest.mark.parametrize("persistence_fails", [False, True])
def test_explicit_api_enabled_commit_wins_but_failed_save_preserves_mqtt_override(
    runtime_factory, monkeypatch, method, url, desired, persistence_fails,
):
    configuration = make_config()
    configuration["valves"][0]["enabled"] = desired
    instance = runtime_factory(configuration=configuration)
    assert instance.mqtt.processMessages(
        "fixturePi/enabled/Valve_A/command", b"0" if desired else b"1",
    )
    assert instance.valves["Valve A"].enabled is not desired
    assert instance.cfg.get_data()["valves"][0]["enabled"] is desired
    before, pulses, options = disk_state(instance), calls(instance), []
    update_config = instance.update_config

    def checked_update(mutator, **kwargs):
        options.append(kwargs)
        return update_config(mutator, **kwargs)

    def fail(*args, **kwargs):
        raise OSError("injected persistence failure")

    monkeypatch.setattr(instance, "update_config", checked_update)
    if persistence_fails:
        monkeypatch.setattr(instance.cfg, "_persist", fail)
    with TestClient(api_server.app, raise_server_exceptions=False) as client:
        response = client.request(method, url)
    assert options == [{"enabled_updates": {"Valve A": desired}}]
    if persistence_fails:
        assert response.status_code == 503
        assert instance.valves["Valve A"].enabled is not desired
        assert instance.controller._runtime_enabled == {"Valve A": not desired}
        assert disk_state(instance) == before
    else:
        assert response.status_code == 200, response.text
        assert instance.valves["Valve A"].enabled is desired
        assert instance.controller._runtime_enabled == {}
        assert instance.cfg.get_data()["valves"][0]["enabled"] is desired
    assert calls(instance) == pulses


@pytest.mark.parametrize("persistence_fails", [False, True])
def test_mqtt_disable_during_file_io_is_preserved_at_atomic_publication(
    client, runtime, monkeypatch, persistence_fails,
):
    entered, release = threading.Event(), threading.Event()
    persist = runtime.cfg._persist
    apply_overrides = runtime.controller.apply_runtime_enabled_updates
    callbacks = []
    runtime.queueJob(Job(runtime.valves["Valve A"], 5, None))
    before, pulses = disk_state(runtime), calls(runtime)

    def paused_persist(*args, **kwargs):
        assert not runtime.controller.lock._is_owned()
        entered.set()
        assert release.wait(5), "test did not release configuration I/O"
        if persistence_fails:
            raise OSError("injected persistence failure")
        return persist(*args, **kwargs)

    def checked_publication(enabled_updates):
        assert runtime.controller.lock._is_owned()
        callbacks.append(dict(runtime.controller._runtime_enabled))
        return apply_overrides(enabled_updates)

    monkeypatch.setattr(runtime.cfg, "_persist", paused_persist)
    monkeypatch.setattr(runtime.controller, "apply_runtime_enabled_updates", checked_publication)
    with ThreadPoolExecutor(max_workers=1) as executor:
        request = executor.submit(
            client.put, "/api/valves/Valve%20A/schedules/0", json={"duration": 45.5},
        )
        try:
            assert entered.wait(5)
            assert runtime.controller.lock.acquire(timeout=1), "file I/O held actuator lock"
            runtime.controller.lock.release()
            assert runtime.mqtt.processMessages("fixturePi/enabled/Valve_A/command", b"0")
            assert runtime.valves["Valve A"].enabled is False
        finally:
            release.set()
        response = request.result(timeout=5)
    assert runtime.valves["Valve A"].enabled is False
    assert runtime.controller._runtime_enabled == {"Valve A": False}
    assert runtime.controller.operations == {}
    assert runtime.cfg.get_data()["valves"][0]["enabled"] is True
    assert calls(runtime) == pulses
    if persistence_fails:
        assert response.status_code == 503
        assert callbacks == []
        assert disk_state(runtime) == before
        assert len(runtime.controller.queue_snapshot()) == 1
    else:
        assert response.status_code == 200, response.text
        assert callbacks == [{"Valve A": False}]
        assert runtime.controller.queue_snapshot() == []
