import calendar
import math
from datetime import datetime, timedelta

import pytz
from suntime import Sun


def season_for(latitude, date):
  northern = ("Winter", "Spring", "Summer", "Fall")
  index = (date.month % 12) // 3
  return northern[index if latitude >= 0 else (index + 2) % 4]


def should_run(schedule, date, latitude, season=None):
  days = getattr(schedule, "days", [])
  seasons = getattr(schedule, "seasons", [])
  return (
    (not days or calendar.day_abbr[date.weekday()] in days)
    and (not seasons or (season or season_for(latitude, date)) in seasons)
  )


def schedule_time(schedule, date, timezone, latitude, longitude):
  tz = pytz.timezone(timezone)
  local = date
  if isinstance(date, datetime):
    local = date.astimezone(tz) if date.tzinfo else tz.localize(date)
  if schedule.time_based_on == "fixed":
    hours, minutes = (int(value) for value in schedule.fixed_start_time.split(":"))
    naive = datetime(local.year, local.month, local.day, hours, minutes)
    try:
      return tz.localize(naive, is_dst=None)
    except pytz.AmbiguousTimeError:
      return tz.localize(naive, is_dst=True)
    except pytz.NonExistentTimeError:
      return None
  sun = Sun(latitude, longitude)
  naive_date = datetime(local.year, local.month, local.day)
  method = sun.get_sunrise_time if schedule.time_based_on == "sunrise" else sun.get_sunset_time
  result = method(at_date=naive_date, time_zone=tz)
  result = result.replace(year=local.year, month=local.month, day=local.day, second=0, microsecond=0)
  return tz.normalize(result + timedelta(minutes=schedule.offset_minutes))


def uv_factor(uv, adjustments):
  if not adjustments:
    return 1.0
  if not math.isfinite(float(uv)) or uv < 0:
    raise ValueError("UV index must be finite and nonnegative")
  for adjustment in adjustments:
    if uv <= adjustment.max_uv_index:
      return adjustment.multiplier
  return adjustments[-1].multiplier


def duration_seconds(minutes, allow_zero=False):
  try:
    seconds = float(minutes) * 60
    represented = timedelta(seconds=seconds)
  except (OverflowError, TypeError, ValueError) as error:
    raise ValueError("Duration cannot be represented safely") from error
  if not math.isfinite(seconds) or seconds < 0 or (seconds == 0 and not allow_zero):
    raise ValueError("Duration must be finite and positive")
  if seconds > 0 and represented == timedelta(0):
    raise ValueError("Positive duration cannot be represented as a nonzero timedelta")
  return seconds


def adjusted_duration(schedule, factor=None):
  duration = schedule.duration
  if getattr(schedule, "enable_uv_adjustments", False) and factor is not None and factor != 1:
    duration *= factor
  if not math.isfinite(duration) or duration < 0:
    raise ValueError("Adjusted duration must be finite and nonnegative")
  duration_seconds(duration, allow_zero=getattr(schedule, "enable_uv_adjustments", False) and factor == 0)
  return duration
