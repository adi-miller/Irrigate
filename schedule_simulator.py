import pytz
import calendar
from datetime import datetime, timedelta
from suntime import Sun

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
    
  def parse_schedule_options(self, options_str):
    """
    Parse --schedule options string
    Format: --schedule=date:2025-06-15,time:08:30,uv:8,season:Summer,rain:yes
    """
    if not options_str:
      return
    
    parts = options_str.split(',')
    for part in parts:
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
      
      # Override time if specified
      if self.override_time:
        sim_dt = sim_dt.replace(hour=self.override_time.hour, 
                                minute=self.override_time.minute, 
                                second=self.override_time.second,
                                microsecond=0)
      
      return sim_dt
    
    return now
  
  def get_simulation_season(self, lat):
    """Get season for simulation (either override or calculated)"""
    if self.override_season:
      return self.override_season
    
    # Use the simulation date to determine season
    sim_dt = self.get_simulation_datetime()
    month = sim_dt.month
    
    season = None
    if lat >= 0:
      if 3 <= month <= 5:
        season = "Spring"
      elif 6 <= month <= 8:
        season = "Summer"
      elif 9 <= month <= 11:
        season = "Fall"
      elif month == 12 or month <= 2:
        season = "Winter"
    else:
      if 3 <= month <= 5:
        season = "Fall"
      elif 6 <= month <= 8:
        season = "Winter"
      elif 9 <= month <= 11:
        season = "Spring"
      elif month == 12 or month <= 2:
        season = "Summer"
    
    return season
  
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
    Get all jobs that should be triggered for the simulation datetime.
    Similar to Irrigate.getScheduledJobsForToday() but uses simulation overrides.
    """
    sim_now = self.get_simulation_datetime()
    scheduled_jobs = []
    
    for valve_name, valve in self.irrigate.valves.items():
      if not valve.enabled or not valve.schedules:
        continue
        
      for sched in valve.schedules:
        # Check day using simulation date
        todayStr = calendar.day_abbr[sim_now.weekday()]
        if len(sched.days) > 0 and todayStr not in sched.days:
          continue
        
        # Check season using simulation logic
        lat, lon = self.irrigate.cfg.getLatLon()
        season = self.get_simulation_season(lat)
        if len(sched.seasons) > 0 and season not in sched.seasons:
          continue
        
        # Calculate when this job would be queued (using simulation date)
        schedule_time = self.calculate_schedule_time_for_simulation(sched, sim_now)
        
        # Calculate duration with UV adjustments (using simulation UV)
        duration = self.calculate_job_duration_for_simulation(valve, sched)
        
        scheduled_jobs.append({
          'valve_name': valve_name,
          'valve': valve,
          'schedule_time': schedule_time,
          'duration_minutes': duration,
          'schedule': sched
        })
    
    # Sort by scheduled time (queue order)
    scheduled_jobs.sort(key=lambda x: x['schedule_time'])
    return scheduled_jobs
  
  def calculate_schedule_time_for_simulation(self, sched, sim_now):
    """Calculate when a schedule should trigger (using simulation datetime)"""
    startTime = sim_now
    timezone = self.irrigate.cfg.timezone
    
    if sched.time_based_on == 'fixed':
      hours, minutes = sched.fixed_start_time.split(":")
      startTime = startTime.replace(hour=int(hours), minute=int(minutes), 
                                   second=0, microsecond=0)
    else:
      lat, lon = self.irrigate.cfg.getLatLon()
      sun = Sun(lat, lon)
      
      if sched.time_based_on == 'sunrise':
        startTime = sun.get_sunrise_time(at_date=sim_now, 
                                        time_zone=pytz.timezone(timezone))
      elif sched.time_based_on == 'sunset':
        startTime = sun.get_sunset_time(at_date=sim_now, 
                                       time_zone=pytz.timezone(timezone))
      
      startTime = startTime.replace(year=sim_now.year, month=sim_now.month, 
                                   day=sim_now.day, second=0, microsecond=0)
      startTime = startTime + timedelta(minutes=int(sched.offset_minutes))
    
    return startTime
  
  def calculate_job_duration_for_simulation(self, valve, sched):
    """Calculate job duration with UV adjustments (using simulation UV)"""
    jobDuration = sched.duration
    
    if sched.enable_uv_adjustments:
      try:
        if hasattr(valve, 'sensor') and valve.sensor:
          uv = self.get_simulation_uv(valve.sensor)
          factor = self.irrigate.uv_adjustments(uv)
          if factor != 1:
            self.logger.info(f"Job duration changed from '{sched.duration}' to '{jobDuration * factor}' based on UV index {uv}")
            jobDuration *= factor
      except Exception as ex:
        self.logger.error("Error calculating UV adjustment '%s': %s." % (valve.sensor.type if hasattr(valve, 'sensor') else 'unknown', format(ex)))
    
    return jobDuration
  
  def simulate_queue_execution(self, scheduled_jobs):
    """Simulate queue execution to predict actual start/end times"""
    sim_now = self.get_simulation_datetime()
    
    # Track when each worker slot becomes available
    # Start at midnight of simulation date
    midnight = sim_now.replace(hour=0, minute=0, second=0, microsecond=0)
    worker_slots = [midnight for _ in range(self.irrigate.cfg.valvesConcurrency)]
    
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
      
      # Update status based on simulation time
      if sim_now >= actual_end:
        job['status'] = 'completed'
      elif sim_now >= actual_start:
        job['status'] = 'running'
      elif sim_now >= job['schedule_time']:
        job['status'] = 'queued'
      else:
        job['status'] = 'scheduled'
    
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
      print("\nNo irrigation jobs scheduled for this day/time.")
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
      print(f"Simulation time: {sim_now.strftime('%Y-%m-%d %H:%M:%S')}")
      
      # Show day and season info
      lat, lon = self.irrigate.cfg.getLatLon()
      season = self.get_simulation_season(lat)
      day = calendar.day_abbr[sim_now.weekday()]
      print(f"Day: {day}, Season: {season}")
      
      print("\n" + "-"*80)
      
      for i, job in enumerate(schedule, 1):
        print(f"\nJob #{i}: {job['valve_name']}")
        
        # Format schedule time with type (Sunrise/Sunset/Fixed)
        sched = job['schedule']
        if sched.time_based_on == 'sunrise':
          schedule_str = f"Sunrise ({job['schedule_time'].strftime('%H:%M:%S')})"
        elif sched.time_based_on == 'sunset':
          schedule_str = f"Sunset ({job['schedule_time'].strftime('%H:%M:%S')})"
        else:  # fixed
          schedule_str = f"Fixed ({job['schedule_time'].strftime('%H:%M:%S')})"
        
        print(f"  Scheduled:    {schedule_str}")
        print(f"  Actual Start: {job['actual_start'].strftime('%H:%M:%S')}", end="")
        if job['queue_delay_minutes'] > 0:
          print(f" (delayed {job['queue_delay_minutes']:.0f} min)")
        else:
          print()
        print(f"  Actual End:   {job['actual_end'].strftime('%H:%M:%S')}")
        print(f"  Duration:     {job['duration_minutes']:.1f} minutes")
        print(f"  Status:       {job['status']}")
        
        # Show schedule details
        print(f"  Days:         {', '.join(sched.days) if sched.days else 'Every day'}")
        print(f"  Seasons:      {', '.join(sched.seasons) if sched.seasons else 'All seasons'}")
        print(f"  Timing:       {sched.time_based_on}", end="")
        if sched.time_based_on == 'fixed':
          print(f" at {sched.fixed_start_time}")
        else:
          offset = sched.offset_minutes
          print(f" {'+' if offset >= 0 else ''}{offset} minutes")
        print(f"  UV Adjust:    {'Yes' if sched.enable_uv_adjustments else 'No'}")
    
    print("\n" + "="*80 + "\n")
