import logging
from types import SimpleNamespace

import pytest
import requests

from mqtt import Mqtt
from sensors.base_sensor import SensorUnavailable
from sensors.openweathermap_sensor import OpenWeatherMapSensor
from tests.runtime_support import FakeClock, make_config
from tests.test_controller import controller_fixture, namespace
from waterflows import MqttWaterflow, TestWaterflow


class FakeHttp:
  def __init__(self, responses):
    self.responses = iter(responses)
    self.calls = []

  def __call__(self, url, **kwargs):
    self.calls.append((url, kwargs))
    result = next(self.responses)
    if isinstance(result, Exception):
      raise result
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: result)


def weather_fixture(responses, days=2):
  clock = FakeClock()
  cfg = namespace(make_config()["sensors"][0])
  cfg.precipitation.days_to_aggregate = days
  http = FakeHttp(responses)
  sensor = OpenWeatherMapSensor(logging.getLogger("weather-test"), cfg, clock, http)
  sensor.started = True
  return sensor, clock, http


def test_weather_snapshot_is_atomic_and_expires_at_six_hours():
  sensor, clock, http = weather_fixture([
    {"daily": [{"uvi": 3.0}]}, {"precipitation": {"total": 0.5}},
    {"precipitation": {"total": 0.75}},
  ])
  with pytest.raises(SensorUnavailable):
    sensor.getTelemetry()
  assert sensor.refresh()
  assert sensor.getTelemetry(True) == {"uv": 3.0, "recentPrecip": 1.25}
  assert sensor.getTelemetry() == sensor.getTelemetry()
  assert all(call[1]["timeout"] == (5, 10) for call in http.calls)
  revision = sensor.revision
  clock.advance(21599)
  assert sensor.get_health()["available"]
  clock.advance(1)
  assert not sensor.get_health()["available"]
  with pytest.raises(SensorUnavailable):
    sensor.shouldDisable()
  assert sensor.revision == revision


@pytest.mark.parametrize("bad", [
  {}, {"precipitation": {"total": -1}}, {"precipitation": {"total": float("nan")}},
  {"precipitation": {"total": "2"}}, requests.Timeout("fixture timeout"),
])
def test_failed_weather_refresh_keeps_last_complete_snapshot(bad):
  sensor, clock, _ = weather_fixture([
    {"daily": [{"uvi": 3}]}, {"precipitation": {"total": 0.2}},
    {"precipitation": {"total": 0.3}},
    {"daily": [{"uvi": 9}]}, {"precipitation": {"total": 1}}, bad,
  ])
  sensor.refresh()
  original = sensor.snapshot()
  clock.advance(7200)
  with pytest.raises((KeyError, ValueError, requests.RequestException)):
    sensor.refresh()
  assert sensor.snapshot() == original
  assert sensor.get_health()["available"]
  assert sensor.getTelemetry() == {"uv": 3.0, "recentPrecip": 0.5}


def test_weather_bad_forecast_never_creates_fresh_defaults():
  sensor, _, _ = weather_fixture([{"daily": [{"uvi": float("inf")}]}])
  with pytest.raises(ValueError):
    sensor.refresh()
  assert sensor.revision == 0
  assert not sensor.get_health()["available"]


def test_weather_aggregation_change_requires_a_matching_complete_snapshot():
  sensor, _, http = weather_fixture([
    {"daily": [{"uvi": 3}]}, {"precipitation": {"total": 0.2}},
    {"precipitation": {"total": 0.3}},
    {"daily": [{"uvi": 4}]}, {"precipitation": {"total": 0.1}},
  ])
  sensor.refresh()
  sensor.precip_days = 1
  assert not sensor.get_health()["available"]
  assert "different precipitation window" in sensor.get_health()["reason"]
  with pytest.raises(SensorUnavailable):
    sensor.shouldDisable()
  assert sensor.refresh()
  assert sensor.getTelemetry() == {"uv": 4.0, "recentPrecip": 0.1}
  assert len(http.calls) == 5


def test_weather_config_change_during_fetch_cannot_publish_mixed_window_as_healthy():
  sensor, _, http = weather_fixture([
    {"daily": [{"uvi": 3}]}, {"precipitation": {"total": 0.2}},
    {"precipitation": {"total": 0.3}},
  ])

  def reconfigure(url, **kwargs):
    response = http(url, **kwargs)
    sensor.precip_days = 1
    return response

  sensor.http_get = reconfigure
  assert sensor.refresh()
  assert len(http.calls) == 3
  assert not sensor.get_health()["available"]
  with pytest.raises(SensorUnavailable):
    sensor.getTelemetry()


def test_weather_shutdown_during_last_request_does_not_publish_a_new_snapshot():
  sensor, _, http = weather_fixture([
    {"daily": [{"uvi": 3}]}, {"precipitation": {"total": 0.2}},
  ], days=1)

  def stop_after_response(url, **kwargs):
    response = http(url, **kwargs)
    if len(http.calls) == 2:
      sensor.shutdown()
    return response

  sensor.http_get = stop_after_response
  assert not sensor.refresh()
  assert sensor.revision == 0
  assert sensor._snapshot is None


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "bad"])
def test_bad_flow_does_not_change_last_observation(value):
  clock = FakeClock()
  flow = TestWaterflow(logging.getLogger("flow-test"), namespace(make_config()["waterflow"]), clock)
  flow.start()
  flow.setLastLiter_1m(2)
  before = flow.snapshot()
  with pytest.raises((ValueError, TypeError)):
    flow.setLastLiter_1m(value)
  assert flow.snapshot() == before


def test_stale_flow_is_not_zero_and_history_reads_are_pure():
  clock = FakeClock()
  flow = TestWaterflow(logging.getLogger("flow-test"), namespace(make_config()["waterflow"]), clock)
  flow.start()
  assert flow.getHistory() == []
  flow.setLastLiter_1m(2.5)
  history = flow.getHistory()
  clock.advance(61)
  assert flow.lastLiter_1m() == 2.5
  assert not flow.snapshot()["available"]
  assert flow.getHistory() == history
  assert flow.intervals(1000, 1061) == [(1000, 1060.0, 2.5), (1060.0, 1061, None)]


class FakeMqttClient:
  def __init__(self):
    self.calls = []

  def reconnect_delay_set(self, **kwargs):
    self.calls.append(("reconnect", kwargs))

  def connect_async(self, hostname):
    self.calls.append(("connect", hostname))

  def loop_start(self):
    self.calls.append(("start",))
    return 0

  def loop_stop(self):
    self.calls.append(("stop",))

  def disconnect(self):
    self.calls.append(("disconnect",))

  def subscribe(self, topic):
    self.calls.append(("subscribe", topic))


def test_mqtt_startup_never_waits_for_connection_and_resubscribes():
  controller, _, valves, _, _, _ = controller_fixture()
  fake = FakeMqttClient()
  instance = SimpleNamespace(
    cfg=SimpleNamespace(mqttClientName="fixturePi", mqttHostName="broker.invalid"),
    valves=valves, logger=logging.getLogger("mqtt-test"), controller=controller,
    terminated=False, reset_telemetry_cursor=lambda: None,
  )
  mqtt = Mqtt(instance, client_factory=lambda: fake)
  mqtt.start()
  assert not mqtt.mqttStarted
  mqtt.on_connect(fake, None, {}, 0)
  mqtt.on_disconnect(fake, None, 1)
  mqtt.on_connect(fake, None, {}, 0)
  assert len([call for call in fake.calls if call[0] == "subscribe"]) == 8
  assert mqtt.shutdown()


def test_flow_client_readiness_is_separate_from_worker_started():
  fake = FakeMqttClient()
  flow = MqttWaterflow(
    logging.getLogger("flow-mqtt-test"), namespace(make_config()["waterflow"]),
    FakeClock(), client_factory=lambda: fake,
  )
  flow.start()
  assert flow.started
  assert not flow.get_health()["available"]
  flow.on_connect(fake, None, {}, 0)
  flow.setLastLiter_1m(0)
  assert flow.get_health()["available"]
  flow.on_disconnect(fake, None, 1)
  assert not flow.get_health()["available"]
  assert flow.shutdown()


@pytest.mark.parametrize("payload,seconds", [(b"", 1800), (b"1", 60), (b"0.5", 30), (b"50", 1800)])
def test_mqtt_forceopen_numeric_policy(payload, seconds):
  controller, clock, valves, _, _, _ = controller_fixture()
  instance = SimpleNamespace(
    cfg=SimpleNamespace(mqttClientName="fixturePi"), valves=valves,
    logger=logging.getLogger("mqtt-command-test"), controller=controller,
    queueJob=controller.enqueue,
  )
  mqtt = Mqtt(instance)
  assert mqtt.processMessages("fixturePi/forceopen/Valve_A/command", payload)
  assert controller.operations["Valve A"].deadline == clock.monotonic() + seconds


@pytest.mark.parametrize("payload", [b" ", b"{}", b"NaN", b"Infinity", b"-2", b"0", b"one", b"1e999"])
def test_mqtt_invalid_manual_never_pulses(payload, caplog):
  controller, _, valves, _, _, _ = controller_fixture()
  instance = SimpleNamespace(
    cfg=SimpleNamespace(mqttClientName="fixturePi"), valves=valves,
    logger=logging.getLogger("mqtt-command-test"), controller=controller,
  )
  before = list(valves["Valve A"].calls)
  assert not Mqtt(instance).processMessages("fixturePi/forceopen/Valve_A/command", payload)
  assert valves["Valve A"].calls == before
  assert "rejected" in caplog.text
