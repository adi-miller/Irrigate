from test_base import runtime


def test_leak_is_not_resolved_by_stale_or_disconnected_flow(runtime):
  flow = runtime.waterflow
  runtime.clock.advance(60)
  flow.setLastLiter_1m(2)
  runtime._check_leak()
  assert "Leaking" in runtime._tempStatus
  runtime.clock.advance(61)
  runtime._check_leak()
  assert "Leaking" in runtime._tempStatus
  assert flow.lastLiter_1m() == 2
  flow.connected = False
  flow.setLastLiter_1m(0)
  runtime._check_leak()
  assert "Leaking" in runtime._tempStatus
  flow.connected = True
  runtime._check_leak()
  assert "Leaking" not in runtime._tempStatus


def test_leak_exclusion_and_open_valve_intent(runtime):
  from types import SimpleNamespace
  runtime.clock.advance(60)
  runtime.waterflow.setLastLiter_1m(2)
  runtime.alerts.leak_detection_exclusions = [
    SimpleNamespace(time_based_on="fixed", fixed_start_time="10:00", duration=55, days=[], seasons=[]),
  ]
  runtime._check_leak()
  assert "Leaking" not in runtime._tempStatus
  runtime.alerts.leak_detection_exclusions = []
  runtime.controller.start_manual("Valve A", 2)
  runtime._check_leak()
  assert "Leaking" not in runtime._tempStatus


def test_flow_history_is_finite_observed_and_nonmutating(runtime):
  flow = runtime.waterflow
  assert flow.getHistory() == []
  flow.setLastLiter_1m(5.5)
  runtime.clock.advance(61)
  flow.setLastLiter_1m(6.5)
  history = flow.getHistory()
  assert len(history) == 2
  assert history[-1]["value"] == 6.5
  assert flow.getHistory() == history
