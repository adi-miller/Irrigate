from model import Job
from test_base import runtime


def test_schedules_trigger_once_per_minute(runtime):
  for valve in runtime.valves.values():
    valve.schedules[0].fixed_start_time = "10:00"
  runtime._schedule_tick()
  runtime._schedule_tick()
  assert runtime.q.qsize() == 3
  runtime.controller.tick()
  assert sum(valve.is_open for valve in runtime.valves.values()) == 2


def test_disabled_valve_is_not_scheduled(runtime):
  valve = runtime.valves["Valve A"]
  valve.enabled = False
  valve.schedules[0].fixed_start_time = "10:00"
  runtime._schedule_tick()
  assert runtime.q.qsize() == 0
  assert [action for action, _ in valve.calls] == ["close"]


def test_day_and_season_filters(runtime):
  schedule = runtime.valves["Valve A"].schedules[0]
  schedule.days = ["Sun"]
  schedule.seasons = ["Summer"]
  assert runtime.shouldScheduleRun(schedule)
  schedule.days = ["Mon"]
  assert not runtime.shouldScheduleRun(schedule)
  schedule.days = []
  schedule.seasons = ["Winter"]
  assert not runtime.shouldScheduleRun(schedule)
  assert runtime.getSeason(1, 6) == "Summer"
  assert runtime.getSeason(-1, 6) == "Winter"


def test_same_valve_jobs_remain_serialized(runtime):
  valve = runtime.valves["Valve A"]
  runtime.queueJob(Job(valve, 0.1, None))
  runtime.queueJob(Job(valve, 0.1, None))
  runtime.controller.tick()
  runtime.clock.advance(6)
  runtime.controller.tick()
  assert [action for action, _ in valve.calls] == ["close", "open", "close", "open"]
  runtime.clock.advance(6)
  runtime.controller.tick()
  assert valve.secondsDaily == 12
  assert runtime.q.unfinished_tasks == 0
