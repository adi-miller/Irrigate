import json
from pathlib import Path
from types import SimpleNamespace

import requests
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import api_server
import irrigate
import schedule_simulator
from alert_channels.millerbot import MillerBotChannel
from alerts import Alert, AlertType
from tests.contract_support import FrozenDateTime, build_contract_app


FIXTURE = Path(__file__).parent / "fixtures" / "contracts" / "baseline.json"

CASES = [
    ("status", "GET", "/api/status", None),
    ("valves", "GET", "/api/valves", None),
    ("valve", "GET", "/api/valves/Example%20Valve", None),
    ("next_runs", "GET", "/api/next-runs", None),
    ("sensors", "GET", "/api/sensors", None),
    ("queue", "GET", "/api/queue", None),
    ("config", "GET", "/api/config", None),
    ("alert_enabled", "POST", "/api/config/alerts/enabled",
     {"alert_type": "leak", "enabled": False}),
    ("alert_settings", "POST", "/api/config/alerts/settings",
     {"setting": "leak_repeat_minutes", "value": 20}),
    ("flow_settings", "POST", "/api/config/waterflow",
     {"setting": "leak_detection", "value": False}),
    ("sensor_settings", "POST", "/api/config/sensors/Weather",
     {"setting": "precip_threshold", "value": 2.0}),
    ("manual", "POST", "/api/valves/Example%20Valve/start-manual", None),
    ("enqueue", "POST", "/api/valves/Example%20Valve/queue?duration_minutes=2.5", None),
    ("stop", "POST", "/api/valves/Example%20Valve/stop", None),
    ("enable", "POST", "/api/valves/Example%20Valve/enable", None),
    ("disable", "POST", "/api/valves/Example%20Valve/disable", None),
    ("update_schedule", "PUT", "/api/valves/Example%20Valve/schedules/0",
     {"duration": 15}),
    ("create_schedule", "POST", "/api/valves/Example%20Valve/schedules",
     {"time_based_on": "fixed", "fixed_start_time": "13:00", "duration": 20}),
    ("delete_schedule", "DELETE", "/api/valves/Example%20Valve/schedules/1", None),
    ("put_enabled", "PUT", "/api/valves/Example%20Valve/enabled?enabled=false", None),
    ("simulate", "POST", "/api/simulate?date=2025-06-15&time=00:00&days=1", None),
]


def collect_contracts(monkeypatch):
    for module in (api_server, irrigate, schedule_simulator):
        monkeypatch.setattr(module, "datetime", FrozenDateTime)
    result = {
        "baseline_commit": "a5e811ee2253a2f2c264a6cf655bcb12bc0ca222",
        "routes": sorted(
            [method, route.path]
            for route in api_server.app.routes if isinstance(route, APIRoute)
            for method in route.methods
            if route.path != "/api/health"
        ),
        "http": {},
    }
    with TestClient(api_server.app) as client:
        for name, method, url, body in CASES:
            instance = build_contract_app()
            monkeypatch.setattr(api_server, "irrigate_instance", instance)
            api_server.invalidate_next_runs_cache()
            response = client.request(method, url, json=body) if body is not None else client.request(method, url)
            assert response.status_code == 200, (name, response.text)
            result["http"][name] = (
                response.text if name == "simulate" else response.json()
            )
    instance = build_contract_app()
    valve = instance.valves["Example Valve"]
    valve.is_open = True
    valve.handled = True
    valve.secondsRemain = 60
    instance.telemetryValve(valve)
    instance.telemetrySensor("Weather", instance.sensors["Weather"])
    instance.publishStatus()
    instance.mqtt.on_connect(instance.mqtt.mqttClient, None, {}, 0)
    result["mqtt"] = {
        "messages": instance.mqtt.mqttClient.messages,
        "subscriptions": instance.mqtt.mqttClient.subscriptions,
    }
    alert = Alert(
        AlertType.IRREGULAR_FLOW, "Example Valve", FrozenDateTime.now(),
        "Example flow observation", {"actual_lpm": 2.5},
    )
    result["alert"] = alert.to_dict()
    captured = []

    def fake_post(url, **kwargs):
        captured.append({"url": url, **kwargs})
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(requests, "post", fake_post)
    channel = MillerBotChannel(instance.logger, SimpleNamespace(
        url="https://alerts.invalid/proactive", user_id=123,
        api_key="fixture-not-a-secret", role="irrigation",
    ))
    assert channel.send(alert) is True
    result["channel_request"] = captured
    return result


def test_baseline_contracts(monkeypatch):
    actual = collect_contracts(monkeypatch)
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert actual == expected
    assert json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(
        expected, sort_keys=True, allow_nan=False,
    )
