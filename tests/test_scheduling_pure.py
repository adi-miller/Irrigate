import logging
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytz

from schedule_simulator import ScheduleSimulator
from scheduling import adjusted_duration, schedule_time, season_for, should_run, uv_factor
from sensors.base_sensor import TestSensor
from tests.runtime_support import FakeClock, make_config
from tests.test_controller import namespace
from valves import TestValve


def simulation_fixture():
  clock = FakeClock()
  cfg = namespace(make_config())
  logger = logging.getLogger("pure-simulation-test")
  sensor = TestSensor(logger, cfg.sensors[0], clock)
  sensor.start()
  sensor.uv = 9
  valve = TestValve(logger, cfg.valves[0], clock)
  valve.sensor = sensor
  valve.schedules[0].enable_uv_adjustments = True
  config = SimpleNamespace(timezone="UTC", valvesConcurrency=1, getLatLon=lambda: (0.0, 0.0))

  def forbidden(*args, **kwargs):
    raise AssertionError("Simulation called a live calculation or publication path")

  app = SimpleNamespace(
    clock=clock, logger=logger, cfg=config, valves={valve.name: valve},
    getSeason=season_for,
    shouldScheduleRun=lambda sched, check_date=None, check_season=None: should_run(sched, check_date, 0, check_season),
    calculateScheduleTime=lambda sched, now: schedule_time(sched, now, "UTC", 0, 0),
    calculateJobDuration=forbidden,
    publishStatus=forbidden,
  )
  return ScheduleSimulator(app), valve, sensor


def test_uv_zero_and_rain_overrides_are_real_and_side_effect_free():
  simulator, valve, sensor = simulation_fixture()
  simulator.parse_schedule_options("date:2025-06-15,time:00:00,uv:0,rain:yes,days:2")
  jobs = simulator.get_todays_schedule()
  assert len(jobs) == 2
  assert all(job["duration_minutes"] == 1 for job in jobs)
  assert all(job["simulated_open_seconds"] == 0 for job in jobs)
  assert all((job["actual_end"] - job["actual_start"]).total_seconds() == 60 for job in jobs)
  assert sensor.uv == 9 and not sensor.disable
  assert valve.calls == [] and not valve.handled and not valve.is_open
  assert "UV Index: 0.0" in simulator.format_schedule()
  simulator.override_should_disable = False
  assert all(job["simulated_open_seconds"] == 60 for job in simulator.get_todays_schedule())


def test_season_override_applies_to_entire_period():
  simulator, valve, _ = simulation_fixture()
  valve.schedules[0].seasons = ["Winter"]
  simulator.parse_schedule_options("date:2025-06-15,time:00:00,season:Winter,days:3")
  assert len(simulator.get_todays_schedule()) == 3


def test_unavailable_weather_uses_explicit_base_duration_not_fabricated_readings():
  simulator, _, sensor = simulation_fixture()
  sensor.exception = True
  simulator.parse_schedule_options("date:2025-06-15,time:00:00")
  job = simulator.get_todays_schedule()[0]
  assert job["duration_minutes"] == 5
  assert job["simulated_open_seconds"] == 300
  assert "unavailable" in job["weather_note"]


@pytest.mark.parametrize("options", ["uv:nan", "uv:inf", "uv:-1", "days:0",
                                    "rain:maybe", "season:nothing", "date:invalid"])
def test_invalid_simulation_options_fail_explicitly(options):
  simulator, _, _ = simulation_fixture()
  with pytest.raises(ValueError):
    simulator.parse_schedule_options(options)


def test_fixed_times_respect_configured_timezone_and_dst():
  schedule = SimpleNamespace(time_based_on="fixed", fixed_start_time="08:00")
  now = pytz.UTC.localize(datetime(2025, 6, 15, 0))
  result = schedule_time(schedule, now, "Asia/Jerusalem", 32, 34)
  assert result.hour == 8
  assert result.utcoffset().total_seconds() == 10800
  schedule.fixed_start_time = "02:30"
  missing = pytz.UTC.localize(datetime(2025, 3, 9, 12))
  assert schedule_time(schedule, missing, "America/New_York", 40, -74) is None


def test_simulated_duplicate_valve_jobs_never_overlap():
  simulator, valve, _ = simulation_fixture()
  simulator.irrigate.cfg.valvesConcurrency = 2
  valve.schedules.append(valve.schedules[0])
  simulator.parse_schedule_options("date:2025-06-15,time:00:00,uv:1")
  jobs = simulator.get_todays_schedule()
  assert jobs[1]["actual_start"] >= jobs[0]["actual_end"]


@pytest.mark.parametrize("duration,factor", [(1e12, 2), (1, 1e308), (1e308, 2)])
def test_unrepresentable_adjusted_duration_fails_explicitly(duration, factor):
  schedule = SimpleNamespace(duration=duration, enable_uv_adjustments=True)
  with pytest.raises(ValueError):
    adjusted_duration(schedule, factor)


def test_zero_uv_multiplier_suppresses_without_an_arbitrary_duration_cap():
  schedule = SimpleNamespace(duration=45, enable_uv_adjustments=True)
  assert adjusted_duration(schedule, 0) == 0
  assert adjusted_duration(schedule, 2) == 90


@pytest.mark.parametrize("duration", [10, 10.0, 10.5])
def test_unadjusted_or_unit_factor_preserves_legacy_numeric_type(duration):
  schedule = SimpleNamespace(duration=duration, enable_uv_adjustments=True)
  for factor in (None, 1, 1.0):
    assert type(adjusted_duration(schedule, factor)) is type(duration)
    assert adjusted_duration(schedule, factor) == duration
  adjustment = SimpleNamespace(max_uv_index=10, multiplier=2)
  assert type(uv_factor(3, [adjustment])) is int
