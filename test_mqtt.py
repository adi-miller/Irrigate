from model import Job
from test_base import runtime


def test_queue_respects_concurrency_and_fifo(runtime):
  for name in runtime.valves:
    assert runtime.mqtt.processMessages("fixturePi/queue/%s/command" % name.replace(" ", "_"), b"1")
  runtime.controller.tick()
  assert runtime.valves["Valve A"].is_open
  assert runtime.valves["Valve B"].is_open
  assert not runtime.valves["Valve C"].is_open
  runtime.clock.advance(60)
  runtime.controller.tick()
  assert runtime.valves["Valve C"].is_open
  assert [action for action, _ in runtime.valves["Valve C"].calls] == ["close", "open"]


def test_enabled_is_runtime_only_and_disabled_queue_never_opens(runtime):
  assert runtime.mqtt.processMessages("fixturePi/enabled/Valve_A/command", b"0")
  assert runtime.cfg.get_data()["valves"][0]["enabled"]
  runtime.queueJob(Job(runtime.valves["Valve A"], 1, None))
  runtime.controller.tick()
  assert not any(action == "open" for action, _ in runtime.valves["Valve A"].calls)
  runtime.mqtt.processMessages("fixturePi/enabled/Valve_A/command", b"1")
  runtime.queueJob(Job(runtime.valves["Valve A"], 1, None))
  runtime.controller.tick()
  assert runtime.valves["Valve A"].is_open


def test_forceclose_cancels_paused_job_without_clearing_pending(runtime):
  sensor = runtime.sensors["Weather"]
  valve = runtime.valves["Valve A"]
  sensor.disable = True
  runtime.queueJob(Job(valve, 1, valve.schedules[0]))
  runtime.controller.tick()
  runtime.queueJob(Job(runtime.valves["Valve C"], 2, None))
  assert runtime.mqtt.processMessages("fixturePi/forceclose/Valve_A/command", b"legacy trigger")
  sensor.disable = False
  runtime.controller.tick()
  assert not any(action == "open" for action, _ in valve.calls)
  assert runtime.valves["Valve C"].is_open


def test_malformed_commands_do_not_actuate(runtime):
  before = {name: list(valve.calls) for name, valve in runtime.valves.items()}
  for topic, payload in [("", b"1"), ("/", b"1"), ("fixturePi/enabled/Valve_A/command", b"4"),
                         ("fixturePi/queue/Valve_A/command", b"nan"),
                         ("fixturePi/forceopen/unknown/command", b"1")]:
    assert not runtime.mqtt.processMessages(topic, payload)
  assert {name: valve.calls for name, valve in runtime.valves.items()} == before
