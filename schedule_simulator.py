import pytz
import calendar
import math
from datetime import datetime, timedelta
from scheduling import adjusted_duration, uv_factor
from sensors.base_sensor import SensorUnavailable

class ScheduleSimulator:
  """
  Handles irrigation schedule simulation and testing scenarios.
  Allows overriding various conditions to test specific scenarios.
  """
  
  def __init__(self, irrigate):
    self.irrigate = irrigate
    self.logger = irrigate.logger
    
    # Override options for testing scenarios
    self.override_date = None
    self.override_time = None
    self.override_uv = None
    self.override_season = None
    self.override_should_disable = None  # Override sensor.shouldDisable()
    self.simulate_days = 1  # Number of days to simulate (1 = today, 7 = week)
    
  def parse_schedule_options(self, options_str):
    """
    Parse --schedule options string
    Format: --schedule=date:2025-06-15,time:08:30,uv:8,season:Summer,rain:yes,week,days:7
    """
    if not options_str:
      return
    
    parts = options_str.split(',')
    for part in parts:
      part = part.strip()
      
      # Handle standalone 'week' option
      if part.lower() == 'week':
        self.simulate_days = 7
        self.logger.info("Simulating full week (7 days)")
        continue
      
      if ':' not in part:
        raise ValueError("Simulation options must use key:value")
      
      key, value = part.split(':', 1)
      key = key.strip().lower()
      value = value.strip()
      
      try:
        if key == 'date':
          # Format: YYYY-MM-DD or MM-DD (use current year)
          if value.count('-') == 2:
            self.override_date = datetime.strptime(value, '%Y-%m-%d').date()
          else:
            year = self.irrigate.clock.now().year
            self.override_date = datetime.strptime(f"{year}-{value}", '%Y-%m-%d').date()
          self.logger.info(f"Override date: {self.override_date}")
        
        elif key == 'days':
          self.simulate_days = int(value)
          if self.simulate_days <= 0:
            raise ValueError("Simulation days must be positive")
          self.irrigate.clock.now() + timedelta(days=self.simulate_days)
          self.logger.info(f"Simulating {self.simulate_days} days")
          
        elif key == 'time':
          # Format: HH:MM or HH:MM:SS
          if value.count(':') == 1:
            self.override_time = datetime.strptime(value, '%H:%M').time()
          else:
            self.override_time = datetime.strptime(value, '%H:%M:%S').time()
          self.logger.info(f"Override time: {self.override_time}")
          
        elif key == 'uv':
          self.override_uv = float(value)
          if not math.isfinite(self.override_uv) or self.override_uv < 0:
            raise ValueError("UV must be finite and nonnegative")
          self.logger.info(f"Override UV index: {self.override_uv}")
          
        elif key == 'season':
          valid_seasons = ['Spring', 'Summer', 'Fall', 'Winter']
          if value.capitalize() in valid_seasons:
            self.override_season = value.capitalize()
            self.logger.info(f"Override season: {self.override_season}")
          else:
            raise ValueError("Invalid simulation season")
        
        elif key in ['rain', 'weather', 'disable']:
          # Override sensor.shouldDisable() - if rain=yes, sensor should disable irrigation
          if value.lower() not in ['yes', 'true', '1', 'on', 'no', 'false', '0', 'off']:
            raise ValueError("Rain override must be a boolean value")
          self.override_should_disable = value.lower() in ['yes', 'true', '1', 'on']
          self.logger.info(f"Override sensor disable: {self.override_should_disable}")
          
        else:
          raise ValueError("Unknown simulation option: %s" % key)
          
      except (ValueError, OverflowError) as ex:
        self.logger.error("Invalid simulation option '%s': %s", key, ex)
        raise ValueError("Invalid simulation option '%s': %s" % (key, ex)) from ex
  
  def get_simulation_datetime(self):
    """Get the datetime to use for simulation (either override or current)"""
    tz = pytz.timezone(self.irrigate.cfg.timezone)
    now = self.irrigate.clock.now().astimezone(tz)
    
    if self.override_date or self.override_time:
      # Start with current datetime
      sim_dt = now
      
      # Override date if specified
      if self.override_date:
        sim_dt = sim_dt.replace(year=self.override_date.year, 
                                month=self.override_date.month, 
                                day=self.override_date.day)
        # If date is specified but no time, default to 00:00:00
        if not self.override_time:
          sim_dt = sim_dt.replace(hour=0, minute=0, second=0, microsecond=0)
      
      # Override time if specified
      if self.override_time:
        sim_dt = sim_dt.replace(hour=self.override_time.hour, 
                                minute=self.override_time.minute, 
                                second=self.override_time.second,
                                microsecond=0)
      
      try:
        return tz.localize(sim_dt.replace(tzinfo=None), is_dst=None)
      except pytz.AmbiguousTimeError:
        return tz.localize(sim_dt.replace(tzinfo=None), is_dst=True)
    
    return now
  
  def get_week_start_date(self):
    """Get the Sunday of the current week (or override week)"""
    base_date = self.get_simulation_datetime()
    # Get the day of week (0=Monday, 6=Sunday in Python)
    # We want Sunday as start, so adjust
    days_since_sunday = (base_date.weekday() + 1) % 7
    sunday = base_date.date() - timedelta(days=days_since_sunday)
    return pytz.timezone(self.irrigate.cfg.timezone).localize(
      datetime.combine(sunday, datetime.min.time()), is_dst=True,
    )
  
  def get_simulation_season(self, lat):
    """Get season for simulation (either override or calculated)"""
    if self.override_season:
      return self.override_season
    
    # Use irrigate's getSeason method with the simulation date
    sim_dt = self.get_simulation_datetime()
    return self.irrigate.getSeason(lat, sim_dt)
  
  def get_simulation_uv(self, sensor):
    """Get UV index for simulation (either override or from sensor)"""
    if self.override_uv is not None:
      return self.override_uv
    
    # Get from actual sensor
    return sensor.getUv()
  
  def get_simulation_should_disable(self, sensor):
    """Get sensor disable status for simulation (either override or from sensor)"""
    if self.override_should_disable is not None:
      return self.override_should_disable
    
    # Get from actual sensor
    return sensor.shouldDisable()
  
  def get_scheduled_jobs_for_simulation(self):
    """
    Get all jobs that should be triggered for the simulation datetime/period.
    Supports single day or multi-day (week) simulation.
    """
    # Determine the base date for simulation
    if self.simulate_days == 7 and not self.override_date:
      # For week simulation without explicit date, start from Sunday of current week
      base_datetime = self.get_week_start_date()
    else:
      base_datetime = self.get_simulation_datetime()
    
    scheduled_jobs = []
    
    # Get lat/lon once for all schedule checks
    lat, lon = self.irrigate.cfg.getLatLon()
    
    # Loop through each day in the simulation period
    for day_offset in range(self.simulate_days):
      sim_date = base_datetime.date() + timedelta(days=day_offset)
      
      for valve_name, valve in self.irrigate.valves.items():
        if not valve.enabled or not valve.schedules:
          continue
          
        for sched in valve.schedules:
          # Check if schedule should run (day and season validation)
          season = self.override_season or self.irrigate.getSeason(lat, sim_date)
          if not self.irrigate.shouldScheduleRun(sched, check_date=sim_date, check_season=season):
            continue
          
          # Calculate when this job would be queued (using simulation date)
          schedule_time = self.irrigate.calculateScheduleTime(sched, sim_date)
          if schedule_time is None:
            continue
          
          # For single day simulation, filter jobs by time
          # Only include jobs scheduled at or after the simulation time
          if self.simulate_days == 1:
            sim_datetime = self.get_simulation_datetime()
            if schedule_time < sim_datetime:
              continue  # Skip jobs that were scheduled before the simulation time
          
          base_duration = sched.duration
          factor = None
          disabled = False
          weather_note = None
          sensor = getattr(valve, 'sensor', None)
          if sensor and sensor.enabled:
            try:
              disabled = self.get_simulation_should_disable(sensor)
            except SensorUnavailable:
              weather_note = "Weather unavailable; continuing within the scheduled lifetime"
            if getattr(sched, 'enable_uv_adjustments', False):
              try:
                uv = self.get_simulation_uv(sensor)
                factor = uv_factor(uv, getattr(sensor, 'uv_adjustments', []))
              except SensorUnavailable:
                weather_note = "Weather adjustment unavailable; using configured base duration"
          duration = adjusted_duration(sched, factor)
          if duration == 0:
            continue
          
          scheduled_jobs.append({
            'valve_name': valve_name,
            'valve': valve,
            'schedule_time': schedule_time,
            'base_duration': base_duration,
            'duration_minutes': duration,
            'schedule': sched,
            'sim_date': sim_date,
            'sensor_disabled': disabled,
            'weather_note': weather_note
          })
    
    # Sort by scheduled time (queue order)
    scheduled_jobs.sort(key=lambda x: x['schedule_time'])
    return scheduled_jobs
  
  def simulate_queue_execution(self, scheduled_jobs):
    """Simulate queue execution to predict actual start/end times"""
    # Track when each worker slot becomes available
    # For multi-day simulation, start at the beginning of the first day
    tz = pytz.timezone(self.irrigate.cfg.timezone)
    if scheduled_jobs:
      first_job_date = min(job['schedule_time'] for job in scheduled_jobs)
      start_date = first_job_date.date()
    else:
      sim_now = self.get_simulation_datetime()
      start_date = sim_now.date()
    start_time = tz.localize(datetime.combine(start_date, datetime.min.time()), is_dst=True)
    
    worker_slots = [start_time for _ in range(self.irrigate.cfg.valvesConcurrency)]
    valve_available = {}
    dispatch_time = start_time
    
    for job in scheduled_jobs:
      # Find the earliest available worker slot
      earliest_available = min(worker_slots)
      
      # Job can't start before it's scheduled
      actual_start = max(job['schedule_time'], earliest_available, dispatch_time,
                         valve_available.get(job['valve_name'], start_time))
      
      # Calculate end time
      duration_timedelta = timedelta(minutes=job['duration_minutes'])
      actual_end = tz.normalize(actual_start + duration_timedelta)
      
      # Update job with realistic times
      job['actual_start'] = actual_start
      job['actual_end'] = actual_end
      job['queue_delay_minutes'] = (actual_start - job['schedule_time']).total_seconds() / 60
      job['simulated_open_seconds'] = 0 if job.get('sensor_disabled') else job['duration_minutes'] * 60
      dispatch_time = actual_start
      valve_available[job['valve_name']] = actual_end
      
      # Update the worker slot that will handle this job
      worker_idx = worker_slots.index(earliest_available)
      worker_slots[worker_idx] = actual_end
    
    return scheduled_jobs
  
  def get_todays_schedule(self):
    """Returns today's schedule with realistic queue simulation"""
    scheduled_jobs = self.get_scheduled_jobs_for_simulation()
    return self.simulate_queue_execution(scheduled_jobs)
  
  def format_schedule(self):
    """Format schedule output as a string"""
    schedule = self.get_todays_schedule()
    sim_now = self.get_simulation_datetime()
    
    lines = []
    lines.append("")
    lines.append("="*80)
    lines.append("Irrigation Schedule")
    lines.append("="*80)
    
    # Show override info if any
    if any([self.override_date, self.override_time, self.override_uv is not None,
            self.override_season, self.override_should_disable is not None]):
      lines.append("")
      lines.append("Simulation Overrides:")
      if self.override_date:
        lines.append(f"  Date:     {self.override_date}")
      if self.override_time:
        lines.append(f"  Time:     {self.override_time}")
      if self.override_uv is not None:
        lines.append(f"  UV Index: {self.override_uv}")
      if self.override_season:
        lines.append(f"  Season:   {self.override_season}")
      if self.override_should_disable is not None:
        lines.append(f"  Weather sensor disables: {'Yes' if self.override_should_disable else 'No'}")
      lines.append("")
    
    if not schedule:
      lines.append("")
      lines.append("No irrigation jobs scheduled for this period.")
      if self.simulate_days > 1:
        if self.simulate_days == 7 and not self.override_date:
          base_date = self.get_week_start_date()
          end_date = base_date + timedelta(days=6)
          lines.append(f"Period: {base_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        else:
          base_date = self.get_simulation_datetime()
          end_date = base_date + timedelta(days=self.simulate_days - 1)
          lines.append(f"Period: {base_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
      else:
        lines.append(f"Simulation date/time: {sim_now.strftime('%Y-%m-%d %H:%M:%S')}")
      
      # Show why there are no jobs
      lat, lon = self.irrigate.cfg.getLatLon()
      season = self.get_simulation_season(lat)
      day = calendar.day_abbr[sim_now.weekday()]
      lines.append(f"Day of week: {day}")
      lines.append(f"Season: {season}")
    else:
      lines.append("")
      lines.append(f"Max concurrent valves: {self.irrigate.cfg.valvesConcurrency}")
      lines.append(f"Timezone: {self.irrigate.cfg.timezone}")
      
      if self.simulate_days > 1:
        if self.simulate_days == 7 and not self.override_date:
          base_date = self.get_week_start_date()
          end_date = base_date + timedelta(days=6)
          lines.append(f"Simulation period: {base_date.strftime('%a %b %d')} to {end_date.strftime('%a %b %d, %Y')}")
        else:
          base_date = self.get_simulation_datetime()
          end_date = base_date + timedelta(days=self.simulate_days - 1)
          lines.append(f"Simulation period: {base_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
      else:
        lines.append(f"Simulation time: {sim_now.strftime('%Y-%m-%d %H:%M:%S')}")
      
      lines.append("")
      lines.append("-"*80)
      
      # Group jobs by date
      jobs_by_date = {}
      for job in schedule:
        date_key = job['sim_date']
        if date_key not in jobs_by_date:
          jobs_by_date[date_key] = []
        jobs_by_date[date_key].append(job)
      
      job_counter = 1
      for sim_date in sorted(jobs_by_date.keys()):
        jobs = jobs_by_date[sim_date]
        
        # Date header for multi-day
        if self.simulate_days > 1:
          date_obj = datetime.combine(sim_date, datetime.min.time())
          lines.append("-"*80)
          lines.append(f"{calendar.day_abbr[date_obj.weekday()]}, {date_obj.strftime('%B %d, %Y')}")
          lines.append("-"*80)
        
        for job in jobs:
          sched = job['schedule']
          
          # Build compact scheduled line
          # Format: Sunrise+90 (07:15) or Fixed (08:00) or Sunset-30 (18:45)
          if sched.time_based_on == 'sunrise':
            offset = sched.offset_minutes
            if offset == 0:
              timing_str = f"Sunrise ({job['schedule_time'].strftime('%H:%M')})"
            elif offset > 0:
              timing_str = f"Sunrise +{offset} ({job['schedule_time'].strftime('%H:%M')})"
            else:
              timing_str = f"Sunrise {offset} ({job['schedule_time'].strftime('%H:%M')})"
          elif sched.time_based_on == 'sunset':
            offset = sched.offset_minutes
            if offset == 0:
              timing_str = f"Sunset ({job['schedule_time'].strftime('%H:%M')})"
            elif offset > 0:
              timing_str = f"Sunset +{offset} ({job['schedule_time'].strftime('%H:%M')})"
            else:
              timing_str = f"Sunset {offset} ({job['schedule_time'].strftime('%H:%M')})"
          else:  # fixed
            timing_str = f"Fixed ({job['schedule_time'].strftime('%H:%M')})"
          
          # Add days if not all days
          days_str = " everyday"
          if len(sched.days) > 0 and len(sched.days) < 7:
            days_str = " every " + ", ".join(sched.days)
          
          # Add seasons if not all seasons
          seasons_str = ""
          if len(sched.seasons) > 0 and len(sched.seasons) < 4:
            seasons_str = " in " + ", ".join(sched.seasons)
          
          # Add UV adjustment flag
          uv_str = ", UV Adjusted" if sched.enable_uv_adjustments else ""
          
          lines.append("")
          lines.append(f"Job #{job_counter}: {job['valve_name']}")
          lines.append(f"  Scheduled:    {timing_str}{days_str}{seasons_str}{uv_str}")
          if job['queue_delay_minutes'] > 0:
            lines.append(f"  Actual Start: {job['actual_start'].strftime('%H:%M:%S')} (delayed {job['queue_delay_minutes']:.0f} min)")
          else:
            lines.append(f"  Actual Start: {job['actual_start'].strftime('%H:%M:%S')}")
          lines.append(f"  Actual End:   {job['actual_end'].strftime('%H:%M:%S')}")
          
          # Duration: show base duration, and if UV adjusted, show the adjusted value in parentheses
          if job['base_duration'] != job['duration_minutes']:
            lines.append(f"  Duration:     {job['base_duration']:.0f} minutes ({job['duration_minutes']:.0f} minutes with UV adjustment)")
          else:
            lines.append(f"  Duration:     {job['duration_minutes']:.0f} minutes")
          if job.get('sensor_disabled'):
            lines.append("  Status:       Weather-inhibited; waiting consumes the scheduled lifetime")
          if job.get('weather_note'):
            lines.append(f"  Weather:      {job['weather_note']}")
          
          job_counter += 1
    
    lines.append("")
    lines.append("="*80)
    lines.append("")
    
    return "\n".join(lines)
  
  def print_schedule(self):
    """Print formatted schedule output"""
    print(self.format_schedule())
