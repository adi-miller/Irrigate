import pytz
import calendar
from datetime import datetime, timedelta

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
        self.logger.warning(f"Invalid schedule option format: '{part}'. Expected key:value")
        continue
      
      key, value = part.split(':', 1)
      key = key.strip().lower()
      value = value.strip()
      
      try:
        if key == 'date':
          # Format: YYYY-MM-DD or MM-DD (use current year)
          if value.count('-') == 2:
            self.override_date = datetime.strptime(value, '%Y-%m-%d').date()
          else:
            year = datetime.now().year
            self.override_date = datetime.strptime(f"{year}-{value}", '%Y-%m-%d').date()
          self.logger.info(f"Override date: {self.override_date}")
        
        elif key == 'days':
          self.simulate_days = int(value)
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
          self.logger.info(f"Override UV index: {self.override_uv}")
          
        elif key == 'season':
          valid_seasons = ['Spring', 'Summer', 'Fall', 'Winter']
          if value.capitalize() in valid_seasons:
            self.override_season = value.capitalize()
            self.logger.info(f"Override season: {self.override_season}")
          else:
            self.logger.warning(f"Invalid season '{value}'. Must be one of: {', '.join(valid_seasons)}")
        
        elif key in ['rain', 'weather', 'disable']:
          # Override sensor.shouldDisable() - if rain=yes, sensor should disable irrigation
          self.override_should_disable = value.lower() in ['yes', 'true', '1', 'on']
          self.logger.info(f"Override sensor disable: {self.override_should_disable}")
          
        else:
          self.logger.warning(f"Unknown schedule option: '{key}'")
          
      except Exception as ex:
        self.logger.error(f"Error parsing schedule option '{part}': {ex}")
  
  def get_simulation_datetime(self):
    """Get the datetime to use for simulation (either override or current)"""
    now = datetime.now().replace(tzinfo=pytz.timezone(self.irrigate.cfg.timezone))
    
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
      
      return sim_dt
    
    return now
  
  def get_week_start_date(self):
    """Get the Sunday of the current week (or override week)"""
    base_date = self.get_simulation_datetime()
    # Get the day of week (0=Monday, 6=Sunday in Python)
    # We want Sunday as start, so adjust
    days_since_sunday = (base_date.weekday() + 1) % 7
    sunday = base_date - timedelta(days=days_since_sunday)
    return sunday.replace(hour=0, minute=0, second=0, microsecond=0)
  
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
      sim_date = base_datetime + timedelta(days=day_offset)
      
      for valve_name, valve in self.irrigate.valves.items():
        if not valve.enabled or not valve.schedules:
          continue
          
        for sched in valve.schedules:
          # Check if schedule should run (day and season validation)
          season = self.irrigate.getSeason(lat, sim_date) if day_offset > 0 else self.get_simulation_season(lat)
          if not self.irrigate.shouldScheduleRun(sched, check_date=sim_date, check_season=season):
            continue
          
          # Calculate when this job would be queued (using simulation date)
          schedule_time = self.irrigate.calculateScheduleTime(sched, sim_date)
          
          # For single day simulation, filter jobs by time
          # Only include jobs scheduled at or after the simulation time
          if self.simulate_days == 1:
            sim_datetime = self.get_simulation_datetime()
            if schedule_time < sim_datetime:
              continue  # Skip jobs that were scheduled before the simulation time
          
          # Calculate duration with UV adjustments (using simulation UV)
          base_duration = sched.duration
          if sched.enable_uv_adjustments and hasattr(valve, 'sensor') and valve.sensor:
            uv = self.get_simulation_uv(valve.sensor)
            adjusted_duration = self.irrigate.calculateJobDuration(valve, sched, uv_override=uv)
          else:
            adjusted_duration = base_duration
          
          scheduled_jobs.append({
            'valve_name': valve_name,
            'valve': valve,
            'schedule_time': schedule_time,
            'base_duration': base_duration,
            'duration_minutes': adjusted_duration,
            'schedule': sched,
            'sim_date': sim_date.date()  # Store the date for grouping in output
          })
    
    # Sort by scheduled time (queue order)
    scheduled_jobs.sort(key=lambda x: x['schedule_time'])
    return scheduled_jobs
  
  def simulate_queue_execution(self, scheduled_jobs):
    """Simulate queue execution to predict actual start/end times"""
    # Track when each worker slot becomes available
    # For multi-day simulation, start at the beginning of the first day
    if scheduled_jobs:
      first_job_date = min(job['schedule_time'] for job in scheduled_jobs)
      start_time = first_job_date.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
      sim_now = self.get_simulation_datetime()
      start_time = sim_now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    worker_slots = [start_time for _ in range(self.irrigate.cfg.valvesConcurrency)]
    
    for job in scheduled_jobs:
      # Find the earliest available worker slot
      earliest_available = min(worker_slots)
      
      # Job can't start before it's scheduled
      actual_start = max(job['schedule_time'], earliest_available)
      
      # Calculate end time
      duration_timedelta = timedelta(minutes=job['duration_minutes'])
      actual_end = actual_start + duration_timedelta
      
      # Update job with realistic times
      job['actual_start'] = actual_start
      job['actual_end'] = actual_end
      job['queue_delay_minutes'] = (actual_start - job['schedule_time']).total_seconds() / 60
      
      # Update the worker slot that will handle this job
      worker_idx = worker_slots.index(earliest_available)
      worker_slots[worker_idx] = actual_end
    
    return scheduled_jobs
  
  def get_todays_schedule(self):
    """Returns today's schedule with realistic queue simulation"""
    scheduled_jobs = self.get_scheduled_jobs_for_simulation()
    return self.simulate_queue_execution(scheduled_jobs)
  
  def print_schedule(self):
    """Print formatted schedule output"""
    schedule = self.get_todays_schedule()
    sim_now = self.get_simulation_datetime()
    
    print("\n" + "="*80)
    print("IRRIGATION SCHEDULE SIMULATION")
    print("="*80)
    
    # Show override info if any
    if any([self.override_date, self.override_time, self.override_uv, 
            self.override_season, self.override_should_disable is not None]):
      print("\nSIMULATION OVERRIDES:")
      if self.override_date:
        print(f"  Date:     {self.override_date}")
      if self.override_time:
        print(f"  Time:     {self.override_time}")
      if self.override_uv is not None:
        print(f"  UV Index: {self.override_uv}")
      if self.override_season:
        print(f"  Season:   {self.override_season}")
      if self.override_should_disable is not None:
        print(f"  Weather sensor disables: {'Yes' if self.override_should_disable else 'No'}")
      print()
    
    if not schedule:
      print("\nNo irrigation jobs scheduled for this period.")
      if self.simulate_days > 1:
        if self.simulate_days == 7 and not self.override_date:
          base_date = self.get_week_start_date()
          end_date = base_date + timedelta(days=6)
          print(f"Period: {base_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        else:
          base_date = self.get_simulation_datetime()
          end_date = base_date + timedelta(days=self.simulate_days - 1)
          print(f"Period: {base_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
      else:
        print(f"Simulation date/time: {sim_now.strftime('%Y-%m-%d %H:%M:%S')}")
      
      # Show why there are no jobs
      lat, lon = self.irrigate.cfg.getLatLon()
      season = self.get_simulation_season(lat)
      day = calendar.day_abbr[sim_now.weekday()]
      print(f"Day of week: {day}")
      print(f"Season: {season}")
    else:
      print(f"\nMax concurrent valves: {self.irrigate.cfg.valvesConcurrency}")
      print(f"Timezone: {self.irrigate.cfg.timezone}")
      
      if self.simulate_days > 1:
        if self.simulate_days == 7 and not self.override_date:
          base_date = self.get_week_start_date()
          end_date = base_date + timedelta(days=6)
          print(f"Simulation period: {base_date.strftime('%a %b %d')} to {end_date.strftime('%a %b %d, %Y')}")
        else:
          base_date = self.get_simulation_datetime()
          end_date = base_date + timedelta(days=self.simulate_days - 1)
          print(f"Simulation period: {base_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
      else:
        print(f"Simulation time: {sim_now.strftime('%Y-%m-%d %H:%M:%S')}")
      
      print("\n" + "-"*80)
      
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
          print("-"*80)
          print(f"{calendar.day_abbr[date_obj.weekday()]}, {date_obj.strftime('%B %d, %Y')}")
          print("-"*80)
        
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
          
          print(f"\nJob #{job_counter}: {job['valve_name']}")
          print(f"  Scheduled:    {timing_str}{days_str}{seasons_str}{uv_str}")
          print(f"  Actual Start: {job['actual_start'].strftime('%H:%M:%S')}", end="")
          if job['queue_delay_minutes'] > 0:
            print(f" (delayed {job['queue_delay_minutes']:.0f} min)")
          else:
            print()
          print(f"  Actual End:   {job['actual_end'].strftime('%H:%M:%S')}")
          
          # Duration: show base duration, and if UV adjusted, show the adjusted value in parentheses
          if job['base_duration'] != job['duration_minutes']:
            print(f"  Duration:     {job['base_duration']:.0f} minutes ({job['duration_minutes']:.0f} minutes with UV adjustment)")
          else:
            print(f"  Duration:     {job['duration_minutes']:.0f} minutes")
          
          job_counter += 1
    
    print("\n" + "="*80 + "\n")
