import copy
import logging
import queue
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

from alerts import AlertType
from controller import ControlError, ValveController, duration_minutes
from irrigate import Irrigate
from model import Job
from mqtt import Mqtt


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2025, 6, 15, 10, 0, 0)
        return value if tz is None else value.replace(tzinfo=timezone.utc).astimezone(tz)

    @classmethod
    def today(cls):
        return cls.now()


class ContractClock:
    def now(self):
        return FrozenDateTime.now(timezone.utc)

    def monotonic(self):
        return 3600.0


def namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [namespace(item) for item in value]
    return value


class ContractSensor:
    def __init__(self):
        self.name = "Weather"
        self.type = "OpenWeatherMap"
        self.enabled = True
        self.started = True
        self.precip_days = 3
        self.precip_threshold = 1.0
        self.uv_adjustments = []
        self.revision = 1
        self.config = SimpleNamespace(
            name=self.name, type="openweathermap", enabled=True,
            precipitation=SimpleNamespace(days_to_aggregate=3, disable_threshold_mm=1.0),
        )

    def shouldDisable(self):
        return False

    def getFactor(self):
        return 1.0

    def getUv(self):
        return 2.5

    def getTelemetry(self, forced=False):
        return {"uv": 2.5, "recentPrecip": 0.0}

    def snapshot(self):
        return {
            "uv": 2.5, "recentPrecip": 0.0, "received": 3600.0,
            "timestamp": FrozenDateTime.now(timezone.utc),
        }


class ContractFlow:
    enabled = True
    started = True
    type = "mqtt"
    leakdetection = True
    _lastupdate = FrozenDateTime(2025, 6, 15, 10, 0, 0)

    def lastLiter_1m(self):
        return 2.5

    def getHistory(self):
        return [{"timestamp": self._lastupdate.isoformat(), "value": 2.5}]

    def snapshot(self):
        return {
            "value": 2.5, "received": 3600.0, "timestamp": self._lastupdate,
            "available": True, "fresh": True,
        }


class ContractValve:
    def __init__(self, sensor, flow):
        self.name = "Example Valve"
        self.enabled = True
        self.is_open = False
        self.handled = False
        self.secondsDaily = 240
        self.litersDaily = 5.0
        self.secondsRemain = 0
        self.secondsDuration = 0
        self.secondsLast = 120
        self.litersLast = 2.5
        self.sensor = sensor
        self.waterflow = flow
        self.config = SimpleNamespace(name=self.name, type="3wire", enabled=True)
        self.schedules = [
            SimpleNamespace(
                time_based_on="fixed", fixed_start_time=start, duration=10,
                days=[], seasons=[], enable_uv_adjustments=False,
            )
            for start in ("11:00", "12:00")
        ]
        self.baseline_lpm = 2.5
        self.baseline_trend = None
        self.baseline_std_dev = 0.2
        self.baseline_sample_count = 12
        self.calls = []

    def open(self):
        self.calls.append("open")

    def close(self):
        self.calls.append("close")


class RecordingMqttClient:
    def __init__(self):
        self.messages = []
        self.subscriptions = []

    def publish(self, topic, payload):
        self.messages.append({"topic": topic, "payload": payload})
        return SimpleNamespace(rc=0)

    def subscribe(self, topic):
        self.subscriptions.append(topic)


class ContractConfig:
    """In-memory configuration double; persistence is covered by integration tests."""

    def __init__(self, data, valves, sensors, flow):
        self.runtime_lock = threading.RLock()
        self._writer_lock = threading.Lock()
        self.valves, self.sensors, self.waterflow = valves, sensors, flow
        self._publish(copy.deepcopy(data))

    def get_data(self):
        with self.runtime_lock:
            return copy.deepcopy(self._data)

    def getLatLon(self):
        return self.latitude, self.longitude

    def transaction(self, mutator, *, on_publish=None):
        with self._writer_lock:
            data = self.get_data()
            mutator(data)
            with self.runtime_lock:
                self._publish(data)
                if on_publish is not None:
                    on_publish()
            return copy.deepcopy(data)

    def _publish(self, data):
        self._data = data
        self.cfg = namespace(data)
        self.timezone = data["timezone"]
        self.latitude, self.longitude = data["location"]["latitude"], data["location"]["longitude"]
        self.valvesConcurrency = data["max_concurrent_valves"]
        self.telemetry = data["telemetry"]["enabled"]
        self.telemIdleInterval = data["telemetry"]["idle_interval"]
        self.telemActiveInterval = data["telemetry"]["active_interval"]
        self.mqttEnabled = data["mqtt"]["enabled"]
        self.mqttClientName = data["mqtt"]["client_name"]
        self.mqttHostName = data["mqtt"]["hostname"]
        for cfg in self.cfg.valves:
            valve = self.valves[cfg.name]
            valve.config, valve.enabled, valve.schedules = cfg, cfg.enabled, cfg.schedules
        for cfg in self.cfg.sensors:
            sensor = self.sensors[cfg.name]
            sensor.config, sensor.enabled = cfg, cfg.enabled
            sensor.precip_days = cfg.precipitation.days_to_aggregate
            sensor.precip_threshold = cfg.precipitation.disable_threshold_mm
            sensor.uv_adjustments = cfg.uv_adjustments
        self.waterflow.config = self.cfg.waterflow
        self.waterflow.enabled = self.cfg.waterflow.enabled
        self.waterflow.leakdetection = self.cfg.waterflow.leakdetection


class ContractController:
    """Inert controller retaining the baseline's exact observations and queue."""

    snapshot = ValveController.snapshot
    valve_snapshot = ValveController.valve_snapshot
    queue_snapshot = ValveController.queue_snapshot
    apply_runtime_enabled_updates = ValveController.apply_runtime_enabled_updates

    def __init__(self, instance):
        self.lock = threading.RLock()
        self.valves, self.q = instance.valves, instance.q
        self.operations, self._runtime_enabled = {}, {}
        self.faults = {name: None for name in self.valves}

    def enqueue(self, job):
        job.duration = duration_minutes(job.duration)
        self.q.put(job)

    def start_manual(self, name, duration=None):
        if name in self.operations:
            raise ControlError("Already running", 409)
        minutes = duration_minutes(duration, manual=True)
        valve = self.valves[name]
        valve.open()
        valve.is_open, valve.handled = True, True
        valve.secondsDuration = valve.secondsRemain = int(minutes * 60)
        self.operations[name] = SimpleNamespace(no_flow=False)

    def stop(self, name):
        valve = self.valves[name]
        valve.close()
        valve.is_open, valve.handled, valve.secondsRemain = False, False, 0
        self.operations.pop(name, None)

    def tick(self):
        pass


def build_contract_app():
    instance = Irrigate.__new__(Irrigate)
    instance.logger = logging.getLogger("contract-fixture")
    instance.startTime = FrozenDateTime(2025, 6, 15, 9, 0, 0)
    instance.clock = ContractClock()
    instance._start_mono = 0.0
    instance._state_lock = threading.RLock()
    instance._sensor_cursors = {}
    instance._mqtt_generation = 0
    instance._heartbeats = {}
    instance.terminated = False
    instance._status = "OK"
    instance._tempStatus = {}
    instance._intervalDict = {}
    instance._lastAllClosed = None
    instance.workers = []
    sensor = ContractSensor()
    flow = ContractFlow()
    valve = ContractValve(sensor, flow)
    flags = {alert.value: True for alert in AlertType}
    data = {
        "timezone": "UTC", "max_concurrent_valves": 1,
        "location": {"latitude": 0.0, "longitude": 0.0},
        "mqtt": {"enabled": True, "hostname": "broker.invalid", "client_name": "fixturePi"},
        "telemetry": {"enabled": True, "idle_interval": 10, "active_interval": 1},
        "alerts": {
            "enabled": flags, "leak_repeat_minutes": 15, "irregular_flow_threshold": 2.0,
            "leak_detection_exclusions": [], "channels": [],
        },
        "waterflow": {
            "type": "mqtt", "enabled": True, "leakdetection": True,
            "hostname": "broker.invalid", "clientname": "fixtureFlow", "topic": "fixture/flow",
        },
        "sensors": [{
            "name": sensor.name, "type": "openweathermap", "enabled": True,
            "api_key": "offline-weather", "latitude": 0.0, "longitude": 0.0,
            "precipitation": {"days_to_aggregate": 3, "disable_threshold_mm": 1.0},
            "uv_adjustments": [],
        }],
        "valves": [{
            "name": valve.name, "type": "3wire", "enabled": True, "watering_mode": "duration",
            "gpio_on_pin": 2, "gpio_off_pin": 3, "sensor": sensor.name,
            "schedules": [vars(schedule).copy() for schedule in valve.schedules],
        }],
    }
    instance.valves = {valve.name: valve}
    instance.sensors = {sensor.name: sensor}
    instance.waterflow = flow
    instance.cfg = ContractConfig(data, instance.valves, instance.sensors, flow)
    instance.alerts = SimpleNamespace(
        enabled={alert: True for alert in AlertType}, leak_repeat_minutes=15,
        irregular_flow_threshold=2.0, alert=lambda *args, **kwargs: None,
        clear_alert_state=lambda *args, **kwargs: None,
    )
    instance.q = queue.Queue()
    instance.q.put(Job(valve=valve, duration=12.5, sched=None))
    instance.controller = ContractController(instance)
    instance.cfg.runtime_lock = instance.controller.lock

    def reload_alerts():
        data = instance.cfg.get_data()["alerts"]
        instance.alerts.enabled = {AlertType(name): enabled for name, enabled in data["enabled"].items()}
        instance.alerts.leak_repeat_minutes = data["leak_repeat_minutes"]
        instance.alerts.irregular_flow_threshold = data["irregular_flow_threshold"]

    instance.alerts.reload_config = reload_alerts
    instance.mqtt = Mqtt(instance)
    instance.mqtt.mqttStarted = True
    instance.mqtt.topicPrefix = "fixturePi/"
    instance.mqtt.mqttClient = RecordingMqttClient()
    return instance
