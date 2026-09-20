import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import config as config_module
from config import Config


class FakeLogger:
    def __init__(self):
        self.messages = []

    def _log(self, level, message, *args):
        self.messages.append((level, message % args if args else str(message)))

    def info(self, message, *args):
        self._log("info", message, *args)

    def warning(self, message, *args):
        self._log("warning", message, *args)

    def error(self, message, *args):
        self._log("error", message, *args)


class FakeFactories:
    def __init__(self):
        self.calls = []

    @staticmethod
    def forbidden(*args, **kwargs):
        raise AssertionError("Configuration must not operate adapters")

    def valve(self, kind, logger, cfg):
        adapter = SimpleNamespace(
            name=cfg.name, enabled=cfg.enabled, config=cfg, schedules=cfg.schedules,
            secondsRemain=123, health={"state": "retained"},
            open=self.forbidden, close=self.forbidden, initialize=self.forbidden,
        )
        self.calls.append(("valve", kind, logger, cfg, adapter))
        return adapter

    def sensor(self, kind, logger, cfg):
        adapter = SimpleNamespace(
            name=cfg.name, enabled=cfg.enabled, config=cfg,
            precip_days=cfg.precipitation.days_to_aggregate,
            precip_threshold=cfg.precipitation.disable_threshold_mm,
            uv_adjustments=cfg.uv_adjustments,
            apiKey=getattr(cfg, "api_key", None),
            lat=getattr(cfg, "latitude", None), lon=getattr(cfg, "longitude", None),
            uv=7.5, recentPrecip=0.75, health={"state": "retained"},
            start=self.forbidden, call_api=self.forbidden,
        )
        self.calls.append(("sensor", kind, logger, cfg, adapter))
        return adapter

    def waterflow(self, kind, logger, cfg):
        adapter = SimpleNamespace(
            type=kind, enabled=cfg.enabled, config=cfg, leakdetection=cfg.leakdetection,
            _lastLiter_1m=4.2, health={"state": "retained"},
            start=self.forbidden, shutdown=self.forbidden,
        )
        self.calls.append(("waterflow", kind, logger, cfg, adapter))
        return adapter

    def keywords(self):
        return {
            "valve_factory": self.valve,
            "sensor_factory": self.sensor,
            "waterflow_factory": self.waterflow,
        }


@pytest.fixture
def source_data():
    return {
        "timezone": "Asia/Jerusalem",
        "max_concurrent_valves": 2,
        "flow_sensor_pin": 18,
        "location": {"latitude": 32.1, "longitude": 34.8},
        "mqtt": {"enabled": False, "hostname": "offline.invalid", "client_name": "irrigate-test"},
        "telemetry": {"enabled": True, "idle_interval": 10, "active_interval": 0.5},
        "alerts": {
            "enabled": {"leak": False},
            "channels": [{
                "type": "millerbot", "url": "https://offline.invalid/notify",
                "user_id": 7, "api_key": "offline-channel-key", "role": "operator",
            }],
            "valve_overrides": {"garden": {"irregular_flow_threshold": 3.25}},
            "leak_detection_exclusions": [{
                "time_based_on": "sunset", "offset_minutes": 15, "duration": 0.5,
            }],
        },
        "sensors": [{
            "type": "openweathermap", "name": "weather", "enabled": True,
            "api_key": "offline-weather-key", "latitude": 32.2, "longitude": 34.9,
            "calibration": {"source": "unchanged", "samples": [1, 2, 3]},
        }],
        "waterflow": {
            "type": "mqtt", "enabled": True, "leakdetection": True,
            "hostname": "offline.invalid", "clientname": "flow-test", "topic": "test/flow",
        },
        "valves": [{
            "name": "garden", "enabled": True, "type": "3wire", "watering_mode": "duration",
            "gpio_on_pin": 5, "gpio_off_pin": 6, "pulse_duration": 0.5, "sensor": "weather",
            "schedules": [
                {"time_based_on": "fixed", "fixed_start_time": "6:05", "duration": 45.5},
                {
                    "time_based_on": "sunrise", "offset_minutes": -30, "duration": 1.25,
                    "enable_uv_adjustments": True,
                },
            ],
        }],
    }


@pytest.fixture
def build_config(tmp_path, source_data):
    def build(data=None, runtime_lock=None, name="config.json"):
        data = copy.deepcopy(source_data if data is None else data)
        path = tmp_path / name
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger, factories = FakeLogger(), FakeFactories()
        cfg = Config(logger, path, runtime_lock=runtime_lock, **factories.keywords())
        return cfg, path, logger, factories
    return build


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def state_snapshot(cfg):
    backup = Path(cfg.last_good_filename)
    return {
        "data": cfg.get_data(),
        "primary": Path(cfg.filename).read_bytes(),
        "backup": backup.read_bytes() if backup.exists() else None,
        "cfg": cfg.cfg,
        "valve": cfg.valves["garden"],
        "valve_config": cfg.valves["garden"].config,
        "schedules": cfg.valves["garden"].schedules,
        "sensor": cfg.sensors["weather"],
        "sensor_config": cfg.sensors["weather"].config,
        "flow": cfg.waterflow,
        "flow_config": cfg.waterflow.config,
    }


def assert_unchanged(cfg, before):
    assert cfg.get_data() == before["data"]
    assert Path(cfg.filename).read_bytes() == before["primary"]
    backup = Path(cfg.last_good_filename)
    assert (backup.read_bytes() if backup.exists() else None) == before["backup"]
    assert cfg.cfg is before["cfg"]
    assert cfg.valves["garden"] is before["valve"]
    assert cfg.valves["garden"].config is before["valve_config"]
    assert cfg.valves["garden"].schedules is before["schedules"]
    assert cfg.sensors["weather"] is before["sensor"]
    assert cfg.sensors["weather"].config is before["sensor_config"]
    assert cfg.waterflow is before["flow"]
    assert cfg.waterflow.config is before["flow_config"]


def test_constructor_normalizes_before_factories_without_rewriting_file(build_config, source_data):
    original = copy.deepcopy(source_data)
    cfg, path, logger, factories = build_config()
    data = cfg.get_data()
    assert read_json(path) == original
    assert source_data == original
    assert not Path(cfg.last_good_filename).exists()
    assert data["sensors"][0]["precipitation"] == {
        "days_to_aggregate": 3, "disable_threshold_mm": 1.0,
    }
    assert data["sensors"][0]["uv_adjustments"] == []
    assert data["alerts"]["leak_repeat_minutes"] == 15
    assert data["alerts"]["irregular_flow_threshold"] == 2.0
    assert data["alerts"]["enabled"]["leak"] is False
    assert all(
        value is True for name, value in data["alerts"]["enabled"].items() if name != "leak"
    )
    assert len(data["alerts"]["enabled"]) == 8
    for window in data["valves"][0]["schedules"] + data["alerts"]["leak_detection_exclusions"]:
        assert window["days"] == []
        assert window["seasons"] == []
    assert data["valves"][0]["schedules"][0]["enable_uv_adjustments"] is False
    assert cfg.valves["garden"].sensor is cfg.sensors["weather"]
    assert [entry[:2] for entry in factories.calls] == [
        ("sensor", "openweathermap"), ("waterflow", "mqtt"), ("valve", "3wire"),
    ]
    assert all(entry[2] is logger for entry in factories.calls)
    assert all(isinstance(entry[3], SimpleNamespace) for entry in factories.calls)
    assert cfg.valves["garden"].config is cfg.cfg.valves[0]
    assert cfg.getLatLon() == (32.1, 34.8)
    assert cfg.valvesConcurrency == 2
    assert cfg.mqttEnabled is False
    assert cfg.mqttHostName == "offline.invalid"
    assert cfg.mqttClientName == "irrigate-test"
    assert cfg.telemetry is True
    assert cfg.telemIdleInterval == 10
    assert cfg.telemActiveInterval == 0.5


def test_optional_sections_and_empty_manual_schedule_normalize(build_config, source_data):
    source_data.pop("sensors")
    source_data.pop("waterflow")
    source_data["alerts"] = {}
    source_data["valves"][0].pop("sensor")
    source_data["valves"][0]["schedules"] = []
    source_data["telemetry"] = {"enabled": False}
    cfg, _, _, factories = build_config(source_data)
    assert cfg.sensors == {}
    assert cfg.waterflow is None
    assert not hasattr(cfg.valves["garden"], "sensor")
    assert cfg.cfg.alerts.leak_detection_exclusions == []
    assert cfg.cfg.alerts.enabled.actuation_failure is True
    assert cfg.cfg.alerts.enabled.safety_intervention is True
    assert cfg.cfg.alerts.enabled.monitoring_unavailable is True
    assert cfg.telemIdleInterval is None
    assert cfg.telemActiveInterval is None
    assert [call[0] for call in factories.calls] == ["valve"]


def test_supported_zero_values_fractional_minutes_and_integral_counts(build_config, source_data):
    source_data["max_concurrent_valves"] = 2.0
    source_data["valves"][0]["gpio_on_pin"] = 5.0
    source_data["valves"][0]["schedules"][1]["offset_minutes"] = -30.0
    source_data["telemetry"]["idle_interval"] = 0
    source_data["telemetry"]["active_interval"] = 0
    source_data["alerts"]["leak_repeat_minutes"] = 0
    source_data["alerts"]["irregular_flow_threshold"] = 0
    source_data["sensors"][0]["precipitation"] = {
        "days_to_aggregate": 0.0, "disable_threshold_mm": 0,
    }
    source_data["sensors"][0]["uv_adjustments"] = [{"max_uv_index": 10, "multiplier": 0}]
    cfg, _, _, _ = build_config(source_data)
    assert cfg.cfg.valves[0].pulse_duration == 0.5
    assert cfg.valves["garden"].schedules[0].duration == 45.5
    assert cfg.valves["garden"].schedules[1].duration == 1.25
    assert cfg.cfg.alerts.leak_detection_exclusions[0].duration == 0.5
    assert cfg.sensors["weather"].uv_adjustments[0].multiplier == 0
    assert type(cfg.valvesConcurrency) is int
    assert type(cfg.cfg.valves[0].gpio_on_pin) is int
    assert type(cfg.valves["garden"].schedules[1].offset_minutes) is int
    assert type(cfg.sensors["weather"].precip_days) is int
    assert cfg.telemIdleInterval == cfg.telemActiveInterval == 0
    assert cfg.cfg.alerts.leak_repeat_minutes == 0


@pytest.mark.parametrize("duration", [31, 45.5, 10_080, 999_999_999 * 1440 + 1439])
def test_representable_schedule_durations_have_no_manual_cap(build_config, duration):
    cfg, path, _, _ = build_config()

    def edit(data):
        data["valves"][0]["schedules"][0]["duration"] = duration
        data["alerts"]["leak_detection_exclusions"][0]["duration"] = duration

    accepted = cfg.transaction(edit)
    assert timedelta(minutes=duration) > timedelta(0)
    assert cfg.valves["garden"].schedules[0].duration == duration
    assert cfg.cfg.alerts.leak_detection_exclusions[0].duration == duration
    assert read_json(path) == accepted


@pytest.mark.parametrize("window_kind", ["schedule", "exclusion"])
@pytest.mark.parametrize("duration", [1e308, 10 ** 100, 1_000_000_000 * 1440])
def test_duration_overflow_rejected_before_persistence_or_factories(
    build_config, tmp_path, source_data, window_kind, duration,
):
    cfg, _, logger, factories = build_config()
    before, calls = state_snapshot(cfg), len(factories.calls)

    def edit(data):
        windows = (
            data["valves"][0]["schedules"] if window_kind == "schedule"
            else data["alerts"]["leak_detection_exclusions"]
        )
        windows[0]["duration"] = duration

    with pytest.raises(ValueError, match="representable as a timedelta"):
        cfg.transaction(edit)
    assert_unchanged(cfg, before)
    assert len(factories.calls) == calls
    assert any(level == "error" for level, _ in logger.messages)

    edit(source_data)
    path = tmp_path / "overflow.json"
    path.write_text(json.dumps(source_data), encoding="utf-8")
    original = path.read_bytes()
    factories, logger = FakeFactories(), FakeLogger()
    with pytest.raises(ValueError, match="representable as a timedelta"):
        Config(logger, path, **factories.keywords())
    assert factories.calls == []
    assert path.read_bytes() == original
    assert any(level == "error" for level, _ in logger.messages)


@pytest.mark.parametrize(("duration", "multiplier"), [
    (1.25, 1e308),
    (100.0, 1e308),
    (1.25, 10 ** 400),
    (1.25, 1_000_000_000 * 1440),
    (999_999_999 * 1440 + 1439, 1.001),
    (1e-8, 1e-320),
])
def test_uv_adjusted_overflow_rejected_before_persistence_or_factories(
    build_config, tmp_path, source_data, duration, multiplier,
):
    cfg, _, logger, factories = build_config()
    before, calls = state_snapshot(cfg), len(factories.calls)
    published = []

    def edit(data):
        data["valves"][0]["schedules"][1]["duration"] = duration
        data["sensors"][0]["uv_adjustments"] = [
            {"max_uv_index": 0, "multiplier": 0},
            {"max_uv_index": 10, "multiplier": multiplier},
        ]

    with pytest.raises(ValueError, match="UV-adjusted duration"):
        cfg.transaction(edit, on_publish=lambda: published.append(True))
    assert_unchanged(cfg, before)
    assert published == []
    assert len(factories.calls) == calls
    assert any(level == "error" for level, _ in logger.messages)
    edit(source_data)
    path = tmp_path / "uv-overflow.json"
    path.write_text(json.dumps(source_data), encoding="utf-8")
    original = path.read_bytes()
    logger, factories = FakeLogger(), FakeFactories()
    with pytest.raises(ValueError, match="UV-adjusted duration"):
        Config(logger, path, **factories.keywords())
    assert factories.calls == []
    assert path.read_bytes() == original
    assert any(level == "error" for level, _ in logger.messages)


@pytest.mark.parametrize(("duration", "multiplier"), [
    (100.0, 0),
    (1e12, 0),
    (45.5, 2.0),
    (1.25, 0.2),
    (999_999_999 * 1440 + 1439, 1.0),
])
def test_valid_uv_adjustments_keep_zero_suppression_and_uncapped_durations(
    build_config, duration, multiplier,
):
    cfg, path, _, factories = build_config()
    calls = len(factories.calls)

    def edit(data):
        data["valves"][0]["schedules"][1]["duration"] = duration
        data["sensors"][0]["uv_adjustments"] = [{"max_uv_index": 10, "multiplier": multiplier}]

    accepted = cfg.transaction(edit)
    assert accepted == cfg.get_data() == read_json(path)
    assert cfg.valves["garden"].schedules[1].duration == duration
    assert cfg.sensors["weather"].uv_adjustments[0].multiplier == multiplier
    assert len(factories.calls) == calls


def test_uv_lifetime_validation_uses_only_the_referenced_sensor(build_config, source_data):
    unused = copy.deepcopy(source_data["sensors"][0])
    unused["name"] = "unused"
    unused["uv_adjustments"] = [{"max_uv_index": 10, "multiplier": 1e308}]
    source_data["sensors"].append(unused)
    source_data["sensors"][0]["uv_adjustments"] = [{"max_uv_index": 10, "multiplier": 2}]
    cfg, _, _, _ = build_config(source_data)
    assert cfg.sensors["unused"].uv_adjustments[0].multiplier == 1e308
    before = state_snapshot(cfg)
    with pytest.raises(ValueError, match="UV-adjusted duration"):
        cfg.transaction(lambda data: data["valves"][0].update(sensor="unused"))
    assert_unchanged(cfg, before)


def test_enabling_uv_revalidates_previously_unused_adjustments(build_config, source_data):
    for schedule in source_data["valves"][0]["schedules"]:
        schedule["enable_uv_adjustments"] = False
    source_data["sensors"][0]["uv_adjustments"] = [{"max_uv_index": 10, "multiplier": 1e308}]
    cfg, _, _, _ = build_config(source_data)
    before = state_snapshot(cfg)
    with pytest.raises(ValueError, match="UV-adjusted duration"):
        cfg.transaction(lambda data: data["valves"][0]["schedules"][0].update(enable_uv_adjustments=True))
    assert_unchanged(cfg, before)


def test_transaction_publishes_existing_objects_only_after_persistence(build_config, monkeypatch):
    cfg, path, _, factories = build_config()
    before = cfg.get_data()
    valve, sensor, flow = cfg.valves["garden"], cfg.sensors["weather"], cfg.waterflow
    valves, sensors = cfg.valves, cfg.sensors
    health = [valve.health, sensor.health, flow.health]
    factory_count = len(factories.calls)
    previous_cfg = cfg.cfg
    observed = []
    staged = cfg._stage_file
    replace = config_module.os.replace

    class PublicationLock:
        held = False

        def __enter__(self):
            assert read_json(path)["valves"][0]["enabled"] is False
            self.held = True
            observed.append("publication")

        def __exit__(self, *args):
            self.held = False

    lock = PublicationLock()
    cfg.runtime_lock = lock

    def checked_stage(*args):
        assert not lock.held
        assert cfg.cfg is previous_cfg
        observed.append("stage")
        return staged(*args)

    def checked_replace(source, destination):
        assert not lock.held
        assert cfg.cfg is previous_cfg
        assert valve.enabled is True
        assert sensor.precip_days == 3
        assert flow.enabled is True
        observed.append("replace")
        return replace(source, destination)

    monkeypatch.setattr(cfg, "_stage_file", checked_stage)
    monkeypatch.setattr(config_module.os, "replace", checked_replace)
    leaked = []

    def edit(data):
        leaked.append(data)
        data["valves"][0]["enabled"] = False
        data["valves"][0]["schedules"] = [{
            "time_based_on": "fixed", "fixed_start_time": "19:05", "duration": 75.25,
        }]
        data["sensors"][0]["enabled"] = False
        data["sensors"][0]["precipitation"] = {
            "days_to_aggregate": 5, "disable_threshold_mm": 2.5,
        }
        data["sensors"][0]["uv_adjustments"] = [{"max_uv_index": 12, "multiplier": 0}]
        data["sensors"][0]["latitude"] = 40
        data["sensors"][0]["longitude"] = -74
        data["sensors"][0]["api_key"] = "replacement-offline-key"
        data["waterflow"]["enabled"] = False
        data["waterflow"]["leakdetection"] = False
        data["alerts"]["enabled"]["actuation_failure"] = False
        data["timezone"] = "UTC"
        data["location"] = {"latitude": 40, "longitude": -74}
        data["max_concurrent_valves"] = 1
        data["mqtt"] = {"enabled": True, "hostname": "other.invalid", "client_name": "changed"}
        data["telemetry"] = {"enabled": False, "idle_interval": 0, "active_interval": 0.25}

    accepted = cfg.transaction(edit)
    assert observed[-1] == "publication"
    assert observed.count("publication") == 1
    assert len(factories.calls) == factory_count
    assert cfg.valves is valves and cfg.sensors is sensors
    assert cfg.valves["garden"] is valve and cfg.sensors["weather"] is sensor
    assert cfg.waterflow is flow
    assert [valve.health, sensor.health, flow.health] == health
    assert valve.secondsRemain == 123
    assert sensor.uv == 7.5 and sensor.recentPrecip == 0.75
    assert flow._lastLiter_1m == 4.2
    assert valve.enabled is False and sensor.enabled is False and flow.enabled is False
    assert valve.config is cfg.cfg.valves[0] and valve.schedules is valve.config.schedules
    assert sensor.config is cfg.cfg.sensors[0] and sensor.uv_adjustments is sensor.config.uv_adjustments
    assert flow.config is cfg.cfg.waterflow and flow.leakdetection is False
    assert valve.sensor is sensor
    assert valve.schedules[0].duration == 75.25
    assert valve.schedules[0].days == valve.schedules[0].seasons == []
    assert sensor.precip_days == 5 and sensor.precip_threshold == 2.5
    assert sensor.apiKey == "replacement-offline-key"
    assert (sensor.lat, sensor.lon) == (40, -74)
    assert sensor.uv_adjustments[0].multiplier == 0
    assert cfg.mqttEnabled is True and cfg.mqttClientName == "changed"
    assert cfg.mqttHostName == "other.invalid" and cfg.timezone == "UTC"
    assert cfg.valvesConcurrency == 1 and cfg.getLatLon() == (40, -74)
    assert cfg.telemIdleInterval == 0 and cfg.telemActiveInterval == 0.25
    assert read_json(path) == accepted
    assert read_json(cfg.last_good_filename) == before
    snapshot = cfg.get_data()
    accepted["alerts"]["enabled"]["leak"] = "not live"
    leaked[0]["sensors"][0]["calibration"]["samples"].append(99)
    assert cfg.get_data() == snapshot
    assert sensor.config.calibration.samples == [1, 2, 3]


def test_publication_hook_runs_after_durability_under_runtime_lock(build_config, monkeypatch):
    cfg, path, _, _ = build_config()
    persist = cfg._persist
    persisted, invoked = {}, []

    def checked_persist(*args, **kwargs):
        assert not cfg.runtime_lock._is_owned()
        persist(*args, **kwargs)
        persisted["data"] = read_json(path)

    def on_publish():
        assert cfg.runtime_lock._is_owned()
        assert cfg._data == persisted["data"]
        assert cfg.cfg.valves[0].schedules[0].duration == 75.25
        assert cfg.valves["garden"].schedules is cfg.cfg.valves[0].schedules
        assert cfg.valves["garden"].enabled is True
        cfg.valves["garden"].enabled = False
        invoked.append("published")

    monkeypatch.setattr(cfg, "_persist", checked_persist)
    accepted = cfg.transaction(
        lambda data: data["valves"][0]["schedules"][0].update(duration=75.25),
        on_publish=on_publish,
    )
    assert invoked == ["published"]
    assert cfg.valves["garden"].enabled is False
    assert accepted == cfg.get_data() == read_json(path)
    assert accepted["valves"][0]["enabled"] is True


def test_noncallable_publication_hook_is_rejected_before_mutation(build_config):
    cfg, _, logger, _ = build_config()
    before, mutations = state_snapshot(cfg), []
    with pytest.raises(TypeError, match="on_publish"):
        cfg.transaction(lambda data: mutations.append(data), on_publish=False)
    assert mutations == []
    assert_unchanged(cfg, before)
    assert any(level == "error" for level, _ in logger.messages)


def test_unexpected_publication_hook_error_is_not_hidden(build_config):
    cfg, path, logger, _ = build_config()

    def on_publish():
        raise RuntimeError("publication callback failed")

    with pytest.raises(RuntimeError, match="publication callback failed"):
        cfg.transaction(lambda data: data["valves"][0].update(enabled=False), on_publish=on_publish)
    assert read_json(path) == cfg.get_data()
    assert cfg.valves["garden"].enabled is False
    assert any("publication callback failed" in message for _, message in logger.messages)


def test_waterflow_enabled_property_is_published_after_persistence(tmp_path, source_data, monkeypatch):
    class PropertyFlow:
        def __init__(self, cfg):
            self.config = cfg
            self.leakdetection = cfg.leakdetection
            self._enabled = cfg.enabled
            self.assignments = []

        @property
        def enabled(self):
            return self._enabled

        @enabled.setter
        def enabled(self, value):
            self.assignments.append(value)
            self._enabled = value

    path = tmp_path / "config.json"
    path.write_text(json.dumps(source_data), encoding="utf-8")
    factories = FakeFactories()
    cfg = Config(
        FakeLogger(), path, valve_factory=factories.valve, sensor_factory=factories.sensor,
        waterflow_factory=lambda kind, logger, settings: PropertyFlow(settings),
    )
    before = state_snapshot(cfg)
    flow = cfg.waterflow

    def fail(*args, **kwargs):
        raise OSError("injected persistence failure")

    with monkeypatch.context() as patch:
        patch.setattr(cfg, "_persist", fail)
        with pytest.raises(OSError):
            cfg.transaction(lambda data: data["waterflow"].update(enabled=False))
    assert_unchanged(cfg, before)
    assert flow.assignments == []
    invoked = []
    cfg.transaction(
        lambda data: data["waterflow"].update(enabled=False),
        on_publish=lambda: invoked.append((flow.enabled, cfg.runtime_lock._is_owned())),
    )
    assert cfg.waterflow is flow
    assert flow.enabled is False and flow.assignments == [False]
    assert invoked == [(False, True)]
    assert "enabled" not in vars(flow)
    cfg.transaction(lambda data: data["alerts"].update(leak_repeat_minutes=20))
    assert flow.assignments == [False]


def test_get_data_and_validation_helper_return_detached_copies(build_config):
    cfg, _, _, _ = build_config()
    before = cfg.get_data()
    supplied = copy.deepcopy(before)
    supplied["valves"][0]["schedules"][0].pop("days")
    normalized = cfg.validate_config_schema(supplied)
    assert "days" not in supplied["valves"][0]["schedules"][0]
    normalized["sensors"][0]["calibration"]["samples"].append(100)
    returned = cfg.get_data()
    returned["sensors"][0]["calibration"]["samples"].clear()
    cfg.cfg.sensors[0].calibration.samples.append(200)
    assert cfg.get_data() == before


_MISSING = object()
_INVALID_CHANGES = [
    (("timezone",), "Invalid/Timezone"),
    (("location", "latitude"), float("nan")),
    (("location", "longitude"), 181),
    (("telemetry", "idle_interval"), -1),
    (("telemetry", "active_interval"), float("inf")),
    (("alerts",), _MISSING),
    (("alerts", "enabled", "leak"), "true"),
    (("alerts", "enabled"), None),
    (("alerts", "leak_repeat_minutes"), float("-inf")),
    (("alerts", "irregular_flow_threshold"), -0.1),
    (("alerts", "valve_overrides"), {"unknown": {"irregular_flow_threshold": 2}}),
    (("alerts", "leak_detection_exclusions", 0, "duration"), _MISSING),
    (("alerts", "leak_detection_exclusions", 0, "duration"), 0),
    (("alerts", "leak_detection_exclusions", 0, "offset_minutes"), 0.25),
    (("sensors",), None),
    (("sensors", 0, "type"), "unknown"),
    (("sensors", 0, "api_key"), _MISSING),
    (("sensors", 0, "precipitation", "days_to_aggregate"), 1.5),
    (("sensors", 0, "precipitation", "disable_threshold_mm"), float("nan")),
    (("sensors", 0, "uv_adjustments"), [{"max_uv_index": 1, "multiplier": -0.1}]),
    (("sensors", 0, "calibration", "samples"), [float("inf")]),
    (("waterflow", "type"), "gpio"),
    (("waterflow", "hostname"), _MISSING),
    (("waterflow", "leakdetection"), _MISSING),
    (("valves", 0, "sensor"), "absent"),
    (("valves", 0, "sensor"), _MISSING),
    (("valves", 0, "type"), "2wire"),
    (("valves", 0, "watering_mode"), "volume"),
    (("valves", 0, "gpio_on_pin"), _MISSING),
    (("valves", 0, "pulse_duration"), 0),
    (("valves", 0, "pulse_duration"), float("nan")),
    (("valves", 0, "schedules", 0, "duration"), 0),
    (("valves", 0, "schedules", 0, "duration"), -1),
    (("valves", 0, "schedules", 0, "duration"), True),
    (("valves", 0, "schedules", 0, "duration"), "2"),
    (("valves", 0, "schedules", 0, "duration"), float("nan")),
    (("valves", 0, "schedules", 0, "duration"), float("inf")),
    (("valves", 0, "schedules", 0, "fixed_start_time"), "24:00"),
    (("valves", 0, "schedules", 0, "fixed_start_time"), "12:60"),
    (("valves", 0, "schedules", 0, "fixed_start_time"), "7:5"),
    (("valves", 0, "schedules", 0, "fixed_start_time"), "7:05\n"),
    (("valves", 0, "schedules", 0, "days"), ["Sunday"]),
    (("valves", 0, "schedules", 0, "seasons"), ["Autumn"]),
    (("valves", 0, "schedules", 1, "offset_minutes"), _MISSING),
    (("valves", 0, "schedules", 1, "offset_minutes"), 0.5),
]


@pytest.mark.parametrize(("path", "value"), _INVALID_CHANGES)
def test_invalid_candidate_does_not_mutate_live_or_disk(build_config, path, value):
    cfg, _, logger, factories = build_config()
    before = state_snapshot(cfg)
    calls = len(factories.calls)

    def edit(data):
        target = data
        for key in path[:-1]:
            target = target[key]
        if value is _MISSING:
            del target[path[-1]]
        else:
            target[path[-1]] = copy.deepcopy(value)

    published = []
    with pytest.raises(ValueError):
        cfg.transaction(edit, on_publish=lambda: published.append(True))
    assert_unchanged(cfg, before)
    assert published == []
    assert len(factories.calls) == calls
    assert any(level == "error" for level, _ in logger.messages)


@pytest.mark.parametrize("category", ["valves", "sensors"])
def test_duplicate_names_rejected_before_any_constructor(tmp_path, source_data, category):
    source_data[category].append(copy.deepcopy(source_data[category][0]))
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(source_data), encoding="utf-8")
    logger, factories = FakeLogger(), FakeFactories()
    with pytest.raises(ValueError, match="Duplicate names"):
        Config(logger, path, **factories.keywords())
    assert factories.calls == []
    assert read_json(path) == source_data
    assert any(level == "error" for level, _ in logger.messages)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_json_constants_rejected_before_any_constructor(tmp_path, source_data, value):
    source_data["valves"][0]["schedules"][0]["duration"] = value
    path = tmp_path / "nonfinite.json"
    path.write_text(json.dumps(source_data), encoding="utf-8")
    before = path.read_bytes()
    logger, factories = FakeLogger(), FakeFactories()
    with pytest.raises(ValueError, match="finite"):
        Config(logger, path, **factories.keywords())
    assert factories.calls == []
    assert path.read_bytes() == before
    assert any(level == "error" for level, _ in logger.messages)


@pytest.mark.parametrize(("category", "kind"), [
    ("valves", "2wire"), ("valves", "volume"), ("waterflow", "gpio"),
])
def test_unimplemented_modes_are_explicit_even_with_injection(
    tmp_path, source_data, category, kind,
):
    if category == "waterflow":
        source_data["waterflow"]["type"] = kind
    else:
        source_data["valves"][0]["watering_mode" if kind == "volume" else "type"] = kind
    path = tmp_path / "unimplemented.json"
    path.write_text(json.dumps(source_data), encoding="utf-8")
    factories = FakeFactories()
    with pytest.raises(ValueError, match="not implemented"):
        Config(FakeLogger(), path, **factories.keywords())
    assert factories.calls == []


@pytest.mark.parametrize("category", ["valves", "sensors", "waterflow"])
def test_test_type_requires_its_corresponding_explicit_factory(
    tmp_path, source_data, monkeypatch, category,
):
    item = source_data[category] if category == "waterflow" else source_data[category][0]
    item["type"] = "test"
    path = tmp_path / "test-type.json"
    path.write_text(json.dumps(source_data), encoding="utf-8")
    factories = FakeFactories()
    monkeypatch.setattr(config_module, "valveFactory", factories.valve)
    monkeypatch.setattr(config_module, "sensorFactory", factories.sensor)
    monkeypatch.setattr(config_module, "waterflowFactory", factories.waterflow)
    keywords = factories.keywords()
    keyword = {"valves": "valve_factory", "sensors": "sensor_factory", "waterflow": "waterflow_factory"}[category]
    keywords.pop(keyword)
    with pytest.raises(ValueError, match="explicit injected factory"):
        Config(FakeLogger(), path, **keywords)
    assert factories.calls == []
    cfg = Config(FakeLogger(), path, **factories.keywords())
    accepted = cfg.get_data()[category]
    assert (accepted if category == "waterflow" else accepted[0])["type"] == "test"
    assert any(call[1] == "test" for call in factories.calls)


def test_default_factories_keep_existing_signature_without_fake_fallback(
    tmp_path, source_data, monkeypatch,
):
    path = tmp_path / "default-factories.json"
    path.write_text(json.dumps(source_data), encoding="utf-8")
    logger, factories = FakeLogger(), FakeFactories()
    monkeypatch.setattr(config_module, "valveFactory", factories.valve)
    monkeypatch.setattr(config_module, "sensorFactory", factories.sensor)
    monkeypatch.setattr(config_module, "waterflowFactory", factories.waterflow)
    cfg = Config(logger, path)
    assert [call[1] for call in factories.calls] == ["openweathermap", "mqtt", "3wire"]
    assert all(call[2] is logger for call in factories.calls)
    assert cfg.valves["garden"] is factories.calls[-1][-1]


def test_schema_is_bundled_not_relative_to_configuration_or_cwd(
    tmp_path, source_data, monkeypatch,
):
    (tmp_path / "config.schema.json").write_text("{}", encoding="utf-8")
    source_data["valves"][0]["schedules"][0]["fixed_start_time"] = "99:99"
    path = tmp_path / "outside.json"
    path.write_text(json.dumps(source_data), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    factories = FakeFactories()
    with pytest.raises(ValueError, match="fixed_start_time"):
        Config(FakeLogger(), path, **factories.keywords())
    assert factories.calls == []


def test_missing_bundled_schema_is_a_logged_error(tmp_path, source_data, monkeypatch):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(source_data), encoding="utf-8")
    monkeypatch.setattr(config_module, "__file__", str(tmp_path / "missing-module" / "config.py"))
    factories, logger = FakeFactories(), FakeLogger()
    with pytest.raises(FileNotFoundError):
        Config(logger, path, **factories.keywords())
    assert factories.calls == []
    assert any("bundled configuration schema" in message for _, message in logger.messages)


def test_preserves_unedited_values_keys_and_explicit_unused_timing_fields(build_config, source_data):
    source_data["valves"][0]["schedules"][0]["offset_minutes"] = 42
    cfg, path, _, _ = build_config(source_data)
    expected = cfg.get_data()
    expected["valves"][0]["enabled"] = False
    result = cfg.transaction(lambda data: data["valves"][0].update(enabled=False))
    assert result == expected
    assert read_json(path) == expected
    assert result["sensors"][0]["calibration"] == source_data["sensors"][0]["calibration"]
    assert result["alerts"]["channels"] == source_data["alerts"]["channels"]
    assert result["alerts"]["valve_overrides"] == source_data["alerts"]["valve_overrides"]
    assert result["valves"][0]["schedules"][0]["offset_minutes"] == 42


def test_duplicate_pins_remain_supported_and_order_changes_reuse_objects(build_config, source_data):
    second = copy.deepcopy(source_data["valves"][0])
    second["name"] = "orchard"
    source_data["valves"].append(second)
    cfg, _, _, factories = build_config(source_data)
    mapping, first, second = cfg.valves, cfg.valves["garden"], cfg.valves["orchard"]
    calls = len(factories.calls)
    cfg.transaction(lambda data: data["valves"].reverse())
    assert cfg.valves is mapping and list(cfg.valves) == ["orchard", "garden"]
    assert cfg.valves["garden"] is first and cfg.valves["orchard"] is second
    assert len(factories.calls) == calls


def test_removing_sensor_reference_and_all_schedules_has_no_stale_binding(build_config):
    cfg, _, _, _ = build_config()
    valve = cfg.valves["garden"]

    def edit(data):
        data["valves"][0].pop("sensor")
        data["valves"][0]["schedules"] = []

    cfg.transaction(edit)
    assert cfg.valves["garden"] is valve
    assert not hasattr(valve, "sensor")
    assert valve.schedules == []


@pytest.mark.parametrize("change", ["new-valve", "new-sensor", "valve-type", "flow-type", "no-flow", "pin", "pulse"])
def test_live_adapter_layout_and_wiring_changes_require_restart(build_config, change):
    cfg, _, _, factories = build_config()
    before, calls = state_snapshot(cfg), len(factories.calls)

    def edit(data):
        if change in ("new-valve", "new-sensor"):
            category = "valves" if change == "new-valve" else "sensors"
            extra = copy.deepcopy(data[category][0])
            extra["name"] = "extra"
            data[category].append(extra)
        elif change == "valve-type":
            data["valves"][0]["type"] = "test"
        elif change == "flow-type":
            data["waterflow"]["type"] = "test"
        elif change == "no-flow":
            data.pop("waterflow")
        elif change == "pin":
            data["valves"][0]["gpio_on_pin"] = 7
        else:
            data["valves"][0]["pulse_duration"] = 0.1

    with pytest.raises(ValueError, match="restart"):
        cfg.transaction(edit)
    assert_unchanged(cfg, before)
    assert len(factories.calls) == calls


def test_mutator_errors_and_nested_transactions_leave_state_untouched(build_config):
    cfg, _, _, _ = build_config()
    before = state_snapshot(cfg)

    def raising(data):
        data["valves"][0]["enabled"] = False
        raise RuntimeError("mutator failed")

    with pytest.raises(RuntimeError, match="mutator failed"):
        cfg.transaction(raising)
    assert_unchanged(cfg, before)
    with pytest.raises(RuntimeError, match="Nested"):
        cfg.transaction(lambda data: cfg.transaction(lambda nested: None))
    assert_unchanged(cfg, before)
    cfg.transaction(lambda data: data["valves"][0].update(enabled=False))
    assert cfg.valves["garden"].enabled is False


@pytest.mark.parametrize("value", [object(), {1: "non-string key"}, (1, 2)])
def test_non_json_candidate_values_are_rejected(build_config, value):
    cfg, _, _, _ = build_config()
    before = state_snapshot(cfg)
    with pytest.raises(ValueError, match="JSON|strings"):
        cfg.transaction(lambda data: data["sensors"][0].update(calibration=value))
    assert_unchanged(cfg, before)


class FailingStream:
    def __init__(self, stream, operation):
        self.stream, self.operation = stream, operation

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def write(self, contents):
        if self.operation == "write":
            self.stream.write(contents[:8])
            raise OSError("injected write failure")
        return self.stream.write(contents)

    def flush(self):
        if self.operation == "flush":
            raise OSError("injected flush failure")
        return self.stream.flush()

    def fileno(self):
        return self.stream.fileno()


@pytest.mark.parametrize("operation", [
    "write", "flush", "fsync", "late-fsync", "backup-replace", "primary-replace", "directory-fsync",
])
def test_persistence_failures_preserve_active_primary_and_backup(
    build_config, tmp_path, monkeypatch, operation,
):
    cfg, _, logger, factories = build_config()
    cfg.transaction(lambda data: data["valves"][0].update(enabled=False))
    before, calls = state_snapshot(cfg), len(factories.calls)
    if operation in ("write", "flush"):
        fdopen = config_module.os.fdopen
        monkeypatch.setattr(
            config_module.os, "fdopen", lambda *args: FailingStream(fdopen(*args), operation),
        )
    elif operation in ("fsync", "late-fsync"):
        fsync = config_module.os.fsync
        count = []

        def failing_fsync(descriptor):
            count.append(descriptor)
            if operation == "fsync" or len(count) == 4:
                raise OSError("injected fsync failure")
            return fsync(descriptor)

        monkeypatch.setattr(config_module.os, "fsync", failing_fsync)
    elif operation == "directory-fsync":
        count = []

        def failing_directory_sync(directory):
            count.append(directory)
            if len(count) == 1:
                raise OSError("injected directory fsync failure")

        monkeypatch.setattr(cfg, "_sync_directory", failing_directory_sync)
    else:
        replace = config_module.os.replace
        target = cfg.filename if operation == "primary-replace" else cfg.last_good_filename

        def failing_replace(source, destination):
            if destination == target:
                raise OSError("injected replace failure")
            return replace(source, destination)

        monkeypatch.setattr(config_module.os, "replace", failing_replace)
    published = []
    with pytest.raises(OSError, match="injected"):
        cfg.transaction(
            lambda data: data["valves"][0].update(enabled=True),
            on_publish=lambda: published.append(True),
        )
    assert_unchanged(cfg, before)
    assert published == []
    assert len(factories.calls) == calls
    assert any("transaction failed" in message for _, message in logger.messages)
    assert sorted(item.name for item in tmp_path.iterdir()) == ["config.json", "config.json.last-good"]


def test_primary_replace_failure_does_not_leave_new_backup(build_config, tmp_path, monkeypatch):
    cfg, _, _, _ = build_config()
    before = state_snapshot(cfg)
    replace = config_module.os.replace

    def failing_replace(source, destination):
        if destination == cfg.filename:
            raise OSError("cannot replace primary")
        return replace(source, destination)

    monkeypatch.setattr(config_module.os, "replace", failing_replace)
    with pytest.raises(OSError, match="cannot replace primary"):
        cfg.transaction(lambda data: data["valves"][0].update(enabled=False))
    assert_unchanged(cfg, before)
    assert list(tmp_path.iterdir()) == [Path(cfg.filename)]


def test_all_file_fsyncs_precede_replace_and_staging_is_same_directory(build_config, monkeypatch):
    cfg, path, _, _ = build_config()
    fsync, replace = config_module.os.fsync, config_module.os.replace
    syncs, replacements = [], []

    def tracked_fsync(descriptor):
        syncs.append(descriptor)
        return fsync(descriptor)

    def tracked_replace(source, destination):
        assert len(syncs) >= 3
        assert Path(source).parent == Path(destination).parent == path.parent
        assert Path(source).name.endswith(".tmp")
        assert json.loads(Path(source).read_bytes())
        replacements.append(destination)
        return replace(source, destination)

    monkeypatch.setattr(config_module.os, "fsync", tracked_fsync)
    monkeypatch.setattr(config_module.os, "replace", tracked_replace)
    cfg.transaction(lambda data: data["valves"][0].update(enabled=False))
    assert replacements == [cfg.last_good_filename, cfg.filename]


def test_concurrent_candidates_serialize_without_holding_actuator_lock(build_config, monkeypatch):
    cfg, path, _, _ = build_config()
    cfg.runtime_lock = threading.Lock()
    original = cfg.get_data()
    blocked, proceed, second_started, second_mutating = [threading.Event() for _ in range(4)]
    staged = cfg._stage_file
    seen = []

    def paused_stage(*args):
        if not blocked.is_set():
            blocked.set()
            assert proceed.wait(5), "test did not release persistence"
        return staged(*args)

    def increment(data):
        seen.append(data["alerts"]["leak_repeat_minutes"])
        data["alerts"]["leak_repeat_minutes"] += 1

    def second_writer():
        second_started.set()

        def second_edit(data):
            second_mutating.set()
            increment(data)

        return cfg.transaction(second_edit)

    monkeypatch.setattr(cfg, "_stage_file", paused_stage)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(cfg.transaction, increment)
        try:
            assert blocked.wait(5)
            second = executor.submit(second_writer)
            assert second_started.wait(5)
            assert not second_mutating.wait(0.05)
            assert cfg.runtime_lock.acquire(timeout=1), "file I/O blocked actuator lock"
            cfg.runtime_lock.release()
            assert cfg.get_data() == original
        finally:
            proceed.set()
        assert first.result(timeout=5)["alerts"]["leak_repeat_minutes"] == 16
        assert second.result(timeout=5)["alerts"]["leak_repeat_minutes"] == 17
    assert seen == [15, 16]
    assert cfg.cfg.alerts.leak_repeat_minutes == 17
    assert read_json(path)["alerts"]["leak_repeat_minutes"] == 17
    assert read_json(cfg.last_good_filename)["alerts"]["leak_repeat_minutes"] == 16


def test_last_good_advances_only_from_accepted_data(build_config):
    cfg, _, _, _ = build_config()
    first = cfg.get_data()
    second = cfg.transaction(lambda data: data["valves"][0].update(enabled=False))
    assert read_json(cfg.last_good_filename) == first
    cfg.transaction(lambda data: data["alerts"].update(leak_repeat_minutes=22))
    assert read_json(cfg.last_good_filename) == second


def test_corrupt_primary_needs_explicit_validated_startup_recovery(build_config):
    cfg, path, _, _ = build_config()
    expected = cfg.get_data()
    cfg.transaction(lambda data: data["valves"][0].update(enabled=False))
    backup = Path(cfg.last_good_filename).read_bytes()
    path.write_text("{broken", encoding="utf-8")
    logger, factories = FakeLogger(), FakeFactories()
    with pytest.raises(json.JSONDecodeError):
        Config(logger, path, **factories.keywords())
    assert path.read_text(encoding="utf-8") == "{broken"
    assert factories.calls == []
    recovered = Config.recover_last_good(logger, path, **factories.keywords())
    assert recovered.get_data() == expected
    assert read_json(path) == expected
    assert Path(cfg.last_good_filename).read_bytes() == backup
    assert recovered.valves["garden"].config is recovered.cfg.valves[0]
    assert len(factories.calls) == 3
    assert any(level == "warning" and "Recovered" in message for level, message in logger.messages)


@pytest.mark.parametrize("failure", ["missing", "json", "semantic", "nonfinite"])
def test_bad_recovery_copy_is_logged_and_never_published(build_config, failure):
    cfg, path, _, _ = build_config()
    cfg.transaction(lambda data: data["valves"][0].update(enabled=False))
    backup = Path(cfg.last_good_filename)
    if failure == "missing":
        backup.unlink()
    elif failure == "json":
        backup.write_text("{broken", encoding="utf-8")
    else:
        data = read_json(backup)
        if failure == "semantic":
            data["timezone"] = "Unknown/Timezone"
        else:
            data["telemetry"]["active_interval"] = float("nan")
        backup.write_text(json.dumps(data), encoding="utf-8")
    before = state_snapshot(cfg)
    logger, factories = FakeLogger(), FakeFactories()
    with pytest.raises((ValueError, FileNotFoundError)):
        Config.recover_last_good(logger, path, **factories.keywords())
    assert_unchanged(cfg, before)
    assert factories.calls == []
    assert any("recovery failed" in message for _, message in logger.messages)


def test_recovery_write_failure_restores_primary_and_constructs_nothing(build_config, monkeypatch):
    cfg, path, _, _ = build_config()
    cfg.transaction(lambda data: data["valves"][0].update(enabled=False))
    before = state_snapshot(cfg)
    logger, factories = FakeLogger(), FakeFactories()

    def failing_replace(*args):
        raise OSError("recovery replace failed")

    monkeypatch.setattr(config_module.os, "replace", failing_replace)
    with pytest.raises(OSError, match="recovery replace failed"):
        Config.recover_last_good(logger, path, **factories.keywords())
    assert_unchanged(cfg, before)
    assert factories.calls == []
    assert any("recovery failed" in message for _, message in logger.messages)


def test_rollback_failure_is_explicit_and_retains_recovery_copy(build_config, tmp_path, monkeypatch):
    cfg, path, logger, _ = build_config()
    cfg.transaction(lambda data: data["valves"][0].update(enabled=False))
    previous_data = cfg.get_data()
    primary_bytes = path.read_bytes()
    previous_backup = Path(cfg.last_good_filename).read_bytes()
    replace = config_module.os.replace
    backup_calls = []

    def failing_replace(source, destination):
        if destination == cfg.filename:
            raise OSError("primary unavailable")
        backup_calls.append(destination)
        if len(backup_calls) > 1:
            raise OSError("backup rollback unavailable")
        return replace(source, destination)

    monkeypatch.setattr(config_module.os, "replace", failing_replace)
    with pytest.raises(OSError, match="rollback failed"):
        cfg.transaction(lambda data: data["valves"][0].update(enabled=True))
    assert cfg.get_data() == previous_data
    assert path.read_bytes() == primary_bytes
    recovery_files = list(tmp_path.glob("*.tmp"))
    assert len(recovery_files) == 1
    assert recovery_files[0].read_bytes() == previous_backup
    assert any("recovery copy:" in message for _, message in logger.messages)


def test_legacy_wrapper_preserves_settings_and_never_serializes_runtime_health(build_config):
    cfg, path, _, _ = build_config()
    before = cfg.get_data()
    cfg.valves["garden"].enabled = False
    cfg.valves["garden"].schedules[0].duration = 70.5
    cfg.valves["garden"].schedules[0].next_trigger = "runtime-only"
    cfg.cfg.alerts.enabled.monitoring_unavailable = False
    cfg.cfg.alerts.leak_repeat_minutes = 20
    cfg.cfg.alerts.irregular_flow_threshold = 4
    cfg.cfg.waterflow.leakdetection = False
    cfg.cfg.sensors[0].precipitation.days_to_aggregate = 6
    cfg.cfg.sensors[0].uv_adjustments = [SimpleNamespace(max_uv_index=11, multiplier=0)]
    saved = cfg.save_runtime_config()
    assert saved == cfg.get_data() == read_json(path)
    assert saved["valves"][0]["enabled"] is False
    assert saved["valves"][0]["schedules"][0]["duration"] == 70.5
    assert saved["alerts"]["enabled"]["monitoring_unavailable"] is False
    assert saved["alerts"]["channels"] == before["alerts"]["channels"]
    assert saved["alerts"]["valve_overrides"] == before["alerts"]["valve_overrides"]
    assert saved["alerts"]["leak_detection_exclusions"] == before["alerts"]["leak_detection_exclusions"]
    assert saved["sensors"][0]["calibration"] == before["sensors"][0]["calibration"]
    assert cfg.sensors["weather"].precip_days == 6
    assert cfg.sensors["weather"].uv_adjustments[0].multiplier == 0
    assert cfg.waterflow.leakdetection is False
    assert "next_trigger" not in path.read_text(encoding="utf-8")
    assert "health" not in path.read_text(encoding="utf-8")
    assert "secondsRemain" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("failure", ["validation", "persistence"])
def test_legacy_wrapper_restores_accepted_settings_on_failure(build_config, monkeypatch, failure):
    cfg, path, _, _ = build_config()
    before = cfg.get_data()
    disk = path.read_bytes()
    valve, sensor = cfg.valves["garden"], cfg.sensors["weather"]
    valve.enabled = False
    cfg.cfg.sensors[0].precipitation.days_to_aggregate = 10
    if failure == "validation":
        valve.schedules[0].duration = -1
    else:
        def fail(*args, **kwargs):
            raise OSError("persistence failed")
        monkeypatch.setattr(cfg, "_persist", fail)
    with pytest.raises((ValueError, OSError)):
        cfg.save_runtime_config()
    assert cfg.get_data() == before
    assert path.read_bytes() == disk
    assert cfg.valves["garden"] is valve and cfg.sensors["weather"] is sensor
    assert valve.enabled == before["valves"][0]["enabled"]
    assert valve.schedules[0].duration == before["valves"][0]["schedules"][0]["duration"]
    assert sensor.precip_days == before["sensors"][0]["precipitation"]["days_to_aggregate"]
