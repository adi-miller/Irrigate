from model import Job
from test_base import runtime


def test_fresh_sensor_pause_and_resume_keep_deadline(runtime):
  valve = runtime.valves["Valve A"]
  runtime.queueJob(Job(valve, 1, valve.schedules[0]))
  runtime.controller.tick()
  deadline = runtime.controller.operations[valve.name].deadline
  runtime.clock.advance(10)
  runtime.sensors["Weather"].disable = True
  runtime.controller.tick()
  assert not valve.is_open
  assert valve.handled
  runtime.clock.advance(10)
  runtime.sensors["Weather"].disable = False
  runtime.controller.tick()
  assert valve.is_open
  assert runtime.controller.operations[valve.name].deadline == deadline


def test_adhoc_queue_ignores_schedule_sensor(runtime):
  valve = runtime.valves["Valve A"]
  runtime.sensors["Weather"].disable = True
  runtime.queueJob(Job(valve, 1, None))
  runtime.controller.tick()
  assert valve.is_open


def test_uv_factor_and_unavailable_base_duration(runtime, caplog):
  valve = runtime.valves["Valve A"]
  schedule = valve.schedules[0]
  schedule.enable_uv_adjustments = True
  sensor = runtime.sensors["Weather"]
  sensor.uv = 1
  assert runtime.calculateJobDuration(valve, schedule) == 1
  sensor.exception = True
  assert runtime.calculateJobDuration(valve, schedule) == 5
  assert "Using base duration" in caplog.text
  runtime.queueJob(Job(valve, 1, schedule))
  runtime.controller.tick()
  assert valve.is_open
  runtime.clock.advance(60)
  runtime.controller.tick()
  assert not valve.is_open
