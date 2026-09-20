import logging
import threading
from types import SimpleNamespace

import pytest

from alerts import AlertType
from controller import ControlError, ValveController
from model import Job
from sensors.base_sensor import TestSensor
from tests.runtime_support import FakeClock, make_config
from valves import TestValve, ThreeWireValve
from waterflows import TestWaterflow


def namespace(value):
  if isinstance(value, dict):
    return SimpleNamespace(**{key: namespace(item) for key, item in value.items()})
  if isinstance(value, list):
    return [namespace(item) for item in value]
  return value


class RecordingAlerts:
  def __init__(self):
    self.events = []
    self.cleared = []

  def alert(self, type, message, valve_name=None, data=None, subject=None):
    self.events.append((type, valve_name, subject, message, data))

  def clear_alert_state(self, type, valve_name=None, subject=None):
    self.cleared.append((type, valve_name, subject))


class FaultValve(TestValve):
  def __init__(self, logger, cfg, clock):
    super().__init__(logger, cfg, clock)
    self.fail_close = 0
    self.fail_open = False

  def open(self):
    super().open()
    if self.fail_open:
      raise OSError("injected open failure")

  def close(self):
    super().close()
    if self.fail_close:
      self.fail_close -= 1
      raise OSError("injected close failure")


def controller_fixture(count=1, concurrency=1):
  clock = FakeClock()
  logger = logging.getLogger("controller-test")
  cfg = namespace(make_config(count=count, concurrency=concurrency))
  sensor = TestSensor(logger, cfg.sensors[0], clock)
  sensor.start()
  flow = TestWaterflow(logger, cfg.waterflow, clock)
  flow.start()
  flow.setLastLiter_1m(6)
  valves = {}
  for vcfg in cfg.valves:
    valve = FaultValve(logger, vcfg, clock)
    valve.sensor = sensor
    valves[valve.name] = valve
  alerts = RecordingAlerts()
  controller = ValveController(valves, concurrency, clock, logger, alerts, flow)
  assert controller.reconcile_startup()
  return controller, clock, valves, sensor, flow, alerts


@pytest.mark.parametrize("value,seconds", [(None, 1800), (0.5, 30), (1, 60), (30, 1800), (99, 1800), (1e308, 1800)])
def test_manual_duration_is_bounded_and_server_enforced(value, seconds):
  controller, clock, valves, _, _, alerts = controller_fixture()
  operation = controller.start_manual("Valve A", value)
  assert operation.deadline == clock.monotonic() + seconds
  clock.advance(seconds)
  controller.tick()
  assert not valves["Valve A"].is_open
  assert "Valve A" not in controller.operations
  assert valves["Valve A"].secondsLast == seconds
  assert valves["Valve A"].secondsRemain == 0
  assert not any(event[0] == AlertType.SAFETY_INTERVENTION for event in alerts.events)


@pytest.mark.parametrize("value", [0, -1, True, "", "   ", "NaN", "inf", "-inf", "1e999", {}, '{"duration_minutes":1}'])
def test_invalid_manual_input_never_actuates(value, caplog):
  controller, _, valves, _, _, _ = controller_fixture()
  before = list(valves["Valve A"].calls)
  with pytest.raises(ControlError):
    controller.start_manual("Valve A", value)
  assert valves["Valve A"].calls == before
  assert "Rejected invalid manual duration" in caplog.text


def test_unrepresentable_clock_duration_never_actuates():
  controller, _, valves, _, _, _ = controller_fixture()
  before = list(valves["Valve A"].calls)
  with pytest.raises(ControlError, match="cannot be represented"):
    controller.start_manual("Valve A", 1e-100)
  assert valves["Valve A"].calls == before
  assert controller.operations == {}


def test_invalid_enabled_update_does_not_poison_later_config_publication():
  controller, _, valves, _, _, _ = controller_fixture()
  with pytest.raises(ControlError):
    controller.set_enabled("Unknown", False)
  with pytest.raises(ControlError):
    controller.set_enabled("Valve A", "false")
  controller.apply_runtime_enabled_updates({})
  assert controller._runtime_enabled == {}
  assert valves["Valve A"].enabled is True


def test_open_conflict_requires_close_and_preserves_deadline():
  controller, clock, valves, _, _, _ = controller_fixture()
  operation = controller.start_manual("Valve A", 2)
  calls = list(valves["Valve A"].calls)
  clock.advance(20)
  with pytest.raises(ControlError) as error:
    controller.start_manual("Valve A", 30)
  assert error.value.status_code == 409
  assert operation.deadline == 1120
  assert valves["Valve A"].calls == calls
  controller.stop("Valve A")
  assert controller.start_manual("Valve A", 1).deadline == clock.monotonic() + 60


def test_queue_lifetime_includes_sensor_wait_and_pause():
  controller, clock, valves, sensor, flow, _ = controller_fixture()
  valve = valves["Valve A"]
  sensor.disable = True
  controller.enqueue(Job(valve, 1, valve.schedules[0]))
  controller.tick()
  operation = controller.operations[valve.name]
  deadline = operation.deadline
  assert controller.states[valve.name] == "paused"
  clock.advance(20)
  sensor.disable = False
  controller.tick()
  clock.advance(10)
  sensor.disable = True
  controller.tick()
  assert valve.secondsLast == 10
  clock.advance(29)
  sensor.disable = False
  controller.tick()
  assert operation.deadline == deadline
  clock.advance(1)
  controller.tick()
  assert valve.secondsLast == 11
  assert valve.litersLast == pytest.approx(1.1)
  assert not valve.handled


def test_never_opened_sensor_job_expires_and_stop_cannot_reopen():
  controller, clock, valves, sensor, _, _ = controller_fixture()
  valve = valves["Valve A"]
  sensor.disable = True
  controller.enqueue(Job(valve, 1, valve.schedules[0]))
  controller.tick()
  with pytest.raises(ControlError):
    controller.start_manual(valve.name)
  controller.stop(valve.name)
  sensor.disable = False
  controller.tick()
  assert not any(action == "open" for action, _ in valve.calls)
  sensor.disable = True
  controller.enqueue(Job(valve, 1, valve.schedules[0]))
  controller.tick()
  clock.advance(60)
  controller.tick()
  assert valve.name not in controller.operations
  assert not any(action == "open" for action, _ in valve.calls)
  assert controller.q.unfinished_tasks == 0


def test_manual_overrides_enabled_sensor_and_concurrency_but_queue_does_not():
  controller, _, valves, sensor, _, _ = controller_fixture(count=3)
  valves["Valve A"].enabled = False
  sensor.disable = True
  controller.enqueue(Job(valves["Valve A"], 1, None))
  controller.enqueue(Job(valves["Valve B"], 1, None))
  controller.enqueue(Job(valves["Valve C"], 1, None))
  controller.tick()
  assert not any(action == "open" for action, _ in valves["Valve A"].calls)
  assert valves["Valve B"].is_open
  assert not valves["Valve C"].is_open
  controller.start_manual("Valve A", 1)
  assert valves["Valve A"].is_open
  assert controller.q.qsize() == 1
  controller.stop("Valve B")
  controller.tick()
  assert valves["Valve C"].is_open


def test_queue_has_no_manual_cap_and_wall_jump_does_not_extend_deadline():
  controller, clock, valves, _, _, _ = controller_fixture()
  valve = valves["Valve A"]
  controller.enqueue(Job(valve, 1500, None))
  controller.tick()
  assert valve.secondsDuration == 90000
  deadline = controller.operations[valve.name].deadline
  clock.advance(10, wall_seconds=-86400)
  controller.tick()
  assert controller.operations[valve.name].deadline == deadline
  clock.advance(89990, wall_seconds=172800)
  controller.tick()
  assert not valve.is_open
  assert valve.secondsLast == 90000


def test_failed_close_is_bounded_and_keeps_uncertainty_until_explicit_retry():
  controller, _, valves, _, _, alerts = controller_fixture()
  valve = valves["Valve A"]
  controller.start_manual(valve.name, 1)
  valve.fail_close = 3
  with pytest.raises(ControlError):
    controller.stop(valve.name)
  assert valve.is_open
  assert not controller.ready
  before = list(valve.calls)
  controller.tick()
  assert valve.calls == before
  assert controller.get_health()["valves"][0]["possibly_open"]
  assert any(event[0] == AlertType.ACTUATION_FAILURE for event in alerts.events)
  controller.stop(valve.name)
  assert controller.ready
  assert not valve.is_open


def test_open_failure_attempts_close_even_when_logical_flag_was_closed():
  controller, _, valves, _, _, _ = controller_fixture()
  valve = valves["Valve A"]
  valve.fail_open = True
  with pytest.raises(ControlError):
    controller.start_manual(valve.name)
  assert [action for action, _ in valve.calls][-2:] == ["open", "close"]
  assert not controller.ready


def test_startup_attempts_every_close_and_failure_blocks_all_watering():
  controller, _, valves, _, _, _ = controller_fixture(count=2)
  valves["Valve A"].fail_close = 3
  assert not controller.reconcile_startup()
  assert valves["Valve B"].calls[-1][0] == "close"
  with pytest.raises(ControlError):
    controller.start_manual("Valve B")
  controller.stop("Valve A")
  assert controller.ready


def test_shared_meter_is_not_duplicated_and_paused_time_is_not_counted():
  controller, clock, valves, sensor, _, _ = controller_fixture(count=2, concurrency=2)
  controller.start_manual("Valve A", 1)
  clock.advance(10)
  controller.tick()
  assert valves["Valve A"].litersLast == pytest.approx(1)
  controller.start_manual("Valve B", 1)
  clock.advance(10)
  controller.tick()
  assert valves["Valve A"].litersLast == pytest.approx(1)
  assert valves["Valve B"].litersLast == 0
  assert not controller.operations["Valve A"].complete
  assert not controller.operations["Valve B"].complete


def test_rolling_no_flow_detects_flow_ceasing_and_ignores_unavailable():
  controller, clock, valves, _, flow, alerts = controller_fixture()
  controller.start_manual("Valve A", 3)
  clock.advance(10)
  controller.tick()
  flow.setLastLiter_1m(0)
  clock.advance(30)
  flow.setLastLiter_1m(0)
  controller.tick()
  clock.advance(30)
  controller.tick()
  assert valves["Valve A"].litersLast > 0
  assert any(event[0] == AlertType.MALFUNCTION_NO_FLOW for event in alerts.events)
  alerts.events.clear()
  flow.connected = False
  clock.advance(61)
  controller.tick()
  assert not any(event[0] == AlertType.MALFUNCTION_NO_FLOW for event in alerts.events)


def test_queue_reads_are_nonmutating_and_same_valve_cannot_be_owned_twice():
  controller, _, valves, _, _, _ = controller_fixture(concurrency=2)
  for minutes in (1, 2, 3):
    controller.enqueue(Job(valves["Valve A"], minutes, None))
  original = controller.queue_snapshot()
  assert controller.queue_snapshot() == original
  assert controller.q.unfinished_tasks == 3
  controller.tick()
  assert len(controller.operations) == 1
  assert [job.duration for job in controller.queue_snapshot()] == [2, 3]


def test_concurrent_open_requests_issue_only_one_pulse():
  controller, _, valves, _, _, _ = controller_fixture()
  barrier = threading.Barrier(8)
  results = []

  def opening():
    barrier.wait()
    try:
      controller.start_manual("Valve A", 1)
      results.append("opened")
    except ControlError:
      results.append("conflict")

  threads = [threading.Thread(target=opening) for _ in range(8)]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join(2)
    assert not thread.is_alive()
  assert results.count("opened") == 1
  assert [action for action, _ in valves["Valve A"].calls].count("open") == 1


def test_real_driver_always_lowers_pin_after_failed_high():
  clock = FakeClock()
  calls = []

  class FakeGpio:
    BCM, OUT, LOW, HIGH = 11, 0, 0, 1

    def setmode(self, mode):
      pass

    def setwarnings(self, enabled):
      pass

    def setup(self, pin, mode, initial):
      assert initial == self.LOW

    def output(self, pin, level):
      calls.append((pin, level))
      if level == self.HIGH:
        raise OSError("injected HIGH failure")

  cfg = namespace(make_config()["valves"][0])
  valve = ThreeWireValve(logging.getLogger("pulse-test"), cfg, gpio=FakeGpio(), clock=clock)
  with pytest.raises(OSError):
    valve.open()
  assert calls == [(cfg.gpio_on_pin, 0), (cfg.gpio_off_pin, 0),
                   (cfg.gpio_on_pin, 1), (cfg.gpio_on_pin, 0), (cfg.gpio_off_pin, 0)]


def test_failed_low_prevents_energizing_opposite_coil():
  clock = FakeClock()
  cfg = namespace(make_config()["valves"][0])
  calls = []

  class FakeGpio:
    BCM, OUT, LOW, HIGH = 11, 0, 0, 1
    stuck = False

    def setmode(self, mode):
      pass

    def setwarnings(self, enabled):
      pass

    def setup(self, pin, mode, initial):
      pass

    def output(self, pin, level):
      calls.append((pin, level))
      if pin == cfg.gpio_on_pin and level == self.HIGH:
        self.stuck = True
      if pin == cfg.gpio_on_pin and level == self.LOW and self.stuck:
        raise OSError("injected stuck HIGH")

  valve = ThreeWireValve(logging.getLogger("pulse-test"), cfg, gpio=FakeGpio(), clock=clock)
  with pytest.raises(RuntimeError, match="de-energization"):
    valve.open()
  with pytest.raises(RuntimeError, match="de-energization"):
    valve.close()
  assert (cfg.gpio_off_pin, 1) not in calls
  assert (cfg.gpio_off_pin, 0) in calls


def test_runtime_disable_survives_unrelated_publication_until_explicit_enable():
  controller, _, valves, _, _, _ = controller_fixture()
  controller.set_enabled("Valve A", False)
  with controller.lock:
    valves["Valve A"].enabled = True
    controller.apply_runtime_enabled_updates({})
    assert not valves["Valve A"].enabled
    controller.apply_runtime_enabled_updates({"Valve A": True})
    assert valves["Valve A"].enabled
  controller.start_manual("Valve A", 1)
  assert valves["Valve A"].is_open


def test_reentrant_sensor_cancellation_cannot_reopen_or_restore_countdown():
  controller, _, valves, sensor, _, _ = controller_fixture()
  valve = valves["Valve A"]

  def cancel_during_read():
    controller.stop(valve.name)
    return False

  sensor.shouldDisable = cancel_during_read
  controller.enqueue(Job(valve, 1, valve.schedules[0]))
  controller.tick()
  assert valve.name not in controller.operations
  assert valve.secondsRemain == 0
  assert not any(action == "open" for action, _ in valve.calls)


def test_late_deadline_is_closed_before_safety_intervention():
  controller, clock, valves, _, _, alerts = controller_fixture()
  controller.start_manual("Valve A", 1)
  clock.advance(62)
  controller.tick()
  assert not valves["Valve A"].is_open
  event = next(event for event in alerts.events if event[0] == AlertType.SAFETY_INTERVENTION)
  assert event[4]["close_command_acknowledged"] is True
  assert event[4]["late_seconds"] == 2
