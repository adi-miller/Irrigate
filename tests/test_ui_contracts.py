import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import api_server
from tests.runtime_support import FakeClock, make_app, make_config


UI_FIXTURE = Path(__file__).parent / "fixtures" / "ui" / "healthy-open.json"


def project_ui_state(status, health):
    return {
        "status": {
            "system": {key: status["system"][key] for key in ("status", "current_time")},
            "valves": status["valves"],
            "sensors": status["sensors"],
            "waterflow": status["waterflow"],
        },
        "health": {
            "ready": health["ready"],
            "controller": {
                key: health["controller"][key] for key in ("running", "fault", "last_error")
            },
            "valves": [
                {key: valve[key] for key in (
                    "name", "state", "operation", "fault", "possibly_open",
                    "physical_state", "flow_alarm", "attribution",
                )}
                for valve in health["valves"]
            ],
            "monitoring": health["monitoring"],
        },
    }


def test_offline_healthy_open_matches_node_ui_fixture(tmp_path, monkeypatch):
    configuration = make_config(sensor=False)
    configuration["valves"][0].update(name="North", enabled=False)
    configuration["waterflow"]["enabled"] = False
    clock = FakeClock(datetime(2026, 9, 18, 12, tzinfo=timezone.utc))
    instance = make_app(tmp_path, clock=clock, configuration=configuration)
    monkeypatch.setattr(api_server, "irrigate_instance", instance)
    try:
        instance.controller.start_manual("North", 5)

        async def read_pair():
            return await api_server.get_full_status(), await api_server.get_health()

        status, health = asyncio.run(read_pair())
        assert health["offline_mode"] is True
        assert health["ready"] is True
        assert health["controller"]["fault"] is False
        assert status["valves"][0]["is_open"] is True
        assert status["valves"][0]["handled"] is True
        assert health["valves"][0]["state"] == "open"
        assert health["valves"][0]["operation"] == "manual"
        assert health["valves"][0]["fault"] is None
        assert health["valves"][0]["possibly_open"] is True
        assert health["valves"][0]["physical_state"] == "unverified"
        expected = json.loads(UI_FIXTURE.read_text(encoding="utf-8"))
        assert project_ui_state(status, health) == expected
    finally:
        instance.shutdown("UI contract test complete")
