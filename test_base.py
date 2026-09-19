import pytest

from tests.runtime_support import make_app, make_config


@pytest.fixture
def runtime(tmp_path):
  app = make_app(tmp_path, configuration=make_config(count=3, concurrency=2))
  try:
    yield app
  finally:
    app.shutdown("test cleanup")


def test_initialization_closes_every_fake_valve_before_ready(runtime):
  assert runtime.controller.ready
  for valve in runtime.valves.values():
    assert [action for action, _ in valve.calls] == ["close"]
    assert not valve.is_open
    assert valve.secondsLast == 0
    assert valve.litersLast == 0


def test_termination_closes_manual_and_queued_operations(runtime):
  from model import Job
  runtime.controller.start_manual("Valve A", 2)
  runtime.queueJob(Job(runtime.valves["Valve B"], 2, None))
  runtime.controller.tick()
  runtime.exit_gracefully()
  assert runtime.shutdown("SIGINT/SIGTERM common path")
  assert all(not valve.is_open for valve in runtime.valves.values())
  assert all(valve.calls[-1][0] == "close" for valve in runtime.valves.values())
  assert not runtime.controller.ready


def test_every_x_minutes_uses_monotonic_minutes(runtime):
  assert runtime.everyXMinutes("test", 1, True)
  runtime.clock.advance(59, wall_seconds=86400)
  assert not runtime.everyXMinutes("test", 1, False)
  runtime.clock.advance(1, wall_seconds=-86400)
  assert runtime.everyXMinutes("test", 1, False)
