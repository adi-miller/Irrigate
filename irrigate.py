import sys
import time
import pytz
import model
import queue
import config
import signal
import getopt
import logging
import calendar
import traceback
import threading
from mqtt import Mqtt
from suntime import Sun
from datetime import datetime
from datetime import timedelta
from threading import Thread
from api_server import run_api_server
from valve_metrics import write_daily_summaries, load_baselines
from schedule_simulator import ScheduleSimulator
from alerts import AlertManager, AlertType

def main(argv):
  # Check for --simulate flag (with or without =)
  simulate_flag = False
  simulateOptions = ""
  
  for arg in sys.argv[1:]:
    if arg == "--simulate":
      simulate_flag = True
      break
    elif arg.startswith("--simulate="):
      simulate_flag = True
      simulateOptions = arg.split("=", 1)[1]
      break
  
  # Parse other options normally (filter out --simulate so getopt doesn't complain)
  filtered_args = [arg for arg in sys.argv[1:] if not arg.startswith("--simulate")]
  options, remainder = getopt.getopt(filtered_args, "", ["config=", "test"])

  configFilename = "config.json"
  test = False

  for opt, arg in options:
    if opt == "--config":
      configFilename = arg
    elif opt == "--test":
      test = True

  irrigate = Irrigate(configFilename)

  if simulate_flag:
    simulator = ScheduleSimulator(irrigate)
    simulator.parse_schedule_options(simulateOptions)
    simulator.print_schedule()
    sys.exit(0)

  if test:
    irrigate.logger.info("Entering test mode. CTRL-C to exit...")
    while True:
      for v in irrigate.valves.values():
        v.handler.open()
        time.sleep(0.2)
      time.sleep(3)
      for v in irrigate.valves.values():
        v.handler.close()
        time.sleep(0.2)
      time.sleep(2)

  # Start FastAPI server in background thread
  api_thread = threading.Thread(target=run_api_server, args=(irrigate,))
  api_thread.daemon = True
  api_thread.start()
  
  irrigate.start(False)
  try:
    while not irrigate.terminated:
      time.sleep(1)
  except KeyboardInterrupt:
    irrigate.terminated = True
  irrigate.logger.info("Program terminated. Waiting for all threads to finish...")

class Irrigate:
  def __init__(self, configFilename):

    signal.signal(signal.SIGTERM, self.exit_gracefully)

    self.startTime = datetime.now()
    self.logger = self.getLogger()
    self.logger.info("Reading configuration file '%s'..." % configFilename)
    self.terminated = False
    self._lastAllClosed = None
    self._intervalDict = {}
    self._status = None
    self._tempStatus = {}
    self.alerts = None  # Will be initialized in init()
    self.init(configFilename)
    self.mqtt = Mqtt(self)
    self.createThreads()

  def exit_gracefully(self, *args):
    self.terminated = True
    
    # Alert system exit
    if self.alerts:
      self.alerts.alert(AlertType.SYSTEM_EXIT, "Graceful shutdown (SIGTERM)")
    
    # Close all manually opened valves (is_open but not handled by a job)
    for valve in self.valves.values():
      if valve.is_open and not valve.handled:
        valve.is_open = False
        valve.close()
        self.logger.info(f"Closed manually opened valve '{valve.name}' on shutdown")
    
    # Gracefully shutdown MQTT connections
    if self.mqtt:
      self.mqtt.shutdown()
    if self.waterflow and hasattr(self.waterflow, 'shutdown'):
      self.waterflow.shutdown()

  def start(self, test = True):
    if self.cfg.mqttEnabled:
      self.logger.info("Starting MQTT...")
      self.mqtt.start()

    self.logger.debug("Starting worker threads...")
    for worker in self.workers:
      self.logger.info("Starting worker thread '%s'." % worker.name)
      worker.daemon = test
      worker.start()

    self.logger.debug("Starting sensors...")
    for _sensor in self.sensors.values():
      if _sensor.enabled and not _sensor.started:
        try:
          self.logger.info(f"Starting sensor '{_sensor.config.type}'.")
          _sensor.start()
        except Exception as ex:
          self.setStatus("InitErrSensor")
          self.logger.error(f"Error starting sensor '{_sensor.name}': '{format(ex)}'.")

    self.logger.debug("Starting waterflows...")
    if self.waterflow is not None and self.waterflow.enabled:
      try:
        self.logger.info("Starting waterflow.")
        self.waterflow.start()
      except Exception as ex:
        self.setStatus("InitErrWaterflow")
        self.logger.error(f"Error starting waterflow 'format(ex)'.")

    self.logger.info("Starting timer thread '%s'." % self.timer.name)
    self.timer.start()

    if self._status is None:
      self.setStatus("OK")

  def init(self, cfgFilename):
    self.cfg = config.Config(self.logger, cfgFilename)
    self.valves = self.cfg.valves
    self.sensors = self.cfg.sensors
    self.waterflow = self.cfg.waterflow
    # self.waterflows = self.cfg.waterflows
    self.q = queue.Queue()
    
    # Load valve baselines from historical data
    from valve_metrics import load_baselines
    load_baselines(self.valves, self.logger)
    
    # Initialize alert manager (pass self for schedule evaluation reuse)
    self.alerts = AlertManager(self.logger, self.cfg, self)

  def createThreads(self):
    self.workers = []
    for i in range(self.cfg.valvesConcurrency):
      worker = Thread(target=self.valveThread, args=())
      worker.daemon = False
      worker.name = f"ValveTh{i}"
      self.workers.append(worker)

    self.timer = Thread(target=self.timerThread, args=())
    self.timer.daemon = True
    self.timer.name = "TimerTh"

  def calculateScheduleTime(self, sched, now):
    """Calculate when a schedule should trigger
    
    Args:
        sched: Schedule object with time configuration
        now: datetime to use for schedule calculation
    
    Returns:
        datetime when the schedule should trigger
    """
    timezone = self.cfg.timezone
    
    if sched.time_based_on == 'fixed':
      hours, minutes = sched.fixed_start_time.split(":")
      startTime = now.replace(hour=int(hours), minute=int(minutes), second=0, microsecond=0)
      if not startTime.tzinfo:
        tz = pytz.timezone(timezone)
        startTime = tz.normalize(startTime.replace(tzinfo=tz))
    else:
      lat, lon = self.cfg.getLatLon()
      sun = Sun(lat, lon)
      # Sun library requires naive datetime, so remove tzinfo
      now_naive = now.replace(tzinfo=None) if now.tzinfo else now
      tz = pytz.timezone(timezone)
      
      if self.everyXMinutes("eval_debuger", 60, True):
        self.logger.info(f"***")
        sunrise = sun.get_sunrise_time(at_date=now_naive, time_zone=tz)
        sunrise = sunrise.replace(year=now.year, month=now.month, day=now.day)
        sunset = sun.get_sunset_time(at_date=now_naive, time_zone=tz)
        sunset = sunset.replace(year=now.year, month=now.month, day=now.day)
        self.logger.info(f"*** Sunrise: {sunrise}")
        self.logger.info(f"*** Sunset: {sunset}")
      if sched.time_based_on == 'sunrise':
        startTime = sun.get_sunrise_time(at_date=now_naive, time_zone=tz).replace(second=0, microsecond=0)
      elif sched.time_based_on == 'sunset':
        startTime = sun.get_sunset_time(at_date=now_naive, time_zone=tz).replace(second=0, microsecond=0)
       
      startTime = startTime.replace(year=now.year, month=now.month, day=now.day) # Hack, because sunset returns the wrong day for some reason
      startTime = startTime + timedelta(minutes=int(sched.offset_minutes))
    
    return startTime

  def shouldScheduleRun(self, sched, check_date=None, check_season=None):
    date_to_check = check_date if check_date else datetime.now()
    todayStr = calendar.day_abbr[date_to_check.weekday()]
    if len(sched.days) > 0 and todayStr not in sched.days:
      return False
    
    # Check season
    if len(sched.seasons) > 0:
      if check_season:
        season = check_season
      else:
        lat, lon = self.cfg.getLatLon()
        season = self.getSeason(lat, date_to_check)
      
      if season not in sched.seasons:
        return False
    
    return True

  def evalSched(self, sched, timezone, now):
    """Evaluate if schedule should trigger at the given time"""
    if not self.shouldScheduleRun(sched, check_date=now):
      return False

    startTime = self.calculateScheduleTime(sched, now)

    if startTime == now:
      return True

    return False

  def getSeason(self, lat, date=None):
    """Get season for a given latitude and optional date (defaults to today)"""
    if date is None:
      month = datetime.today().month
    else:
      month = date.month if hasattr(date, 'month') else date
    
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

  def checkIrregularFlow(self, valve, total_seconds, total_liters):
    """Check if flow rate is off baseline at end of valve cycle"""
    # Need baseline data
    if valve.baseline_lpm is None or valve.baseline_std_dev is None:
      return
    
    if total_seconds <= 0:
      return
    
    # Calculate actual flow rate
    actual_lpm = (total_liters / total_seconds) * 60
    
    # Get threshold (default from config or per-valve override)
    threshold = 2.0  # Default std deviations
    if hasattr(self.cfg.cfg, 'alerts'):
      if hasattr(self.cfg.cfg.alerts, 'irregular_flow_threshold'):
        threshold = self.cfg.cfg.alerts.irregular_flow_threshold
      
      # Check for valve-specific override
      if hasattr(self.cfg.cfg.alerts, 'valve_overrides'):
        if hasattr(self.cfg.cfg.alerts.valve_overrides, valve.name):
          valve_override = getattr(self.cfg.cfg.alerts.valve_overrides, valve.name)
          if hasattr(valve_override, 'irregular_flow_threshold'):
            threshold = valve_override.irregular_flow_threshold
    
    # Calculate acceptable range
    lower_bound = valve.baseline_lpm - (valve.baseline_std_dev * threshold)
    upper_bound = valve.baseline_lpm + (valve.baseline_std_dev * threshold)
    
    # Check if outside range
    if actual_lpm < lower_bound or actual_lpm > upper_bound:
      deviation_pct = ((actual_lpm - valve.baseline_lpm) / valve.baseline_lpm) * 100
      direction = "above" if actual_lpm > valve.baseline_lpm else "below"
      
      self.alerts.alert(
        AlertType.IRREGULAR_FLOW,
        f"Valve '{valve.name}' flow rate {direction} baseline: {actual_lpm:.2f} L/min vs baseline {valve.baseline_lpm:.2f} L/min ({deviation_pct:+.1f}%)",
        valve_name=valve.name,
        data={
          "actual_lpm": round(actual_lpm, 2),
          "baseline_lpm": valve.baseline_lpm,
          "baseline_std_dev": valve.baseline_std_dev,
          "threshold_std_devs": threshold,
          "deviation_percent": round(deviation_pct, 2),
          "total_seconds": total_seconds,
          "total_liters": round(total_liters, 2)
        }
      )

  def calculateJobDuration(self, valve, sched):
    """Calculate job duration with sensor factor if applicable"""
    jobDuration = sched.duration
    
    if sched.enable_uv_adjustments and hasattr(valve, 'sensor') and valve.sensor:
      try:
        factor = valve.sensor.getFactor()
        if factor != 1:
          self.logger.debug(f"Job duration adjusted from {sched.duration} to {jobDuration * factor} (factor: {factor}).")
          jobDuration *= factor
        
        self.clearTempStatus("SensorErr")
        if self.alerts:
          self.alerts.clear_alert_state(AlertType.SENSOR_ERROR, valve.sensor.name)
          
      except Exception as ex:
        self.setTempStatus("SensorErr")
        error_msg = format(ex)
        self.logger.error("Error calculating sensor factor '%s': %s." % (valve.sensor.name, error_msg))
        if self.alerts:
          self.alerts.alert(
            AlertType.SENSOR_ERROR,
            f"Sensor '{valve.sensor.name}' error: {error_msg}",
            data={"sensor_name": valve.sensor.name, "error": error_msg}
          )
    
    return jobDuration

  def valveThread(self):
    self.logger.info("Valve handler thread '%s' started." % threading.current_thread().name)
    while not self.terminated:
      try:
        irrigateJob = self.q.get(timeout=5)
        self.logger.info("Thread '%s' picked up job for valve '%s'. Queue size: %s." % (threading.current_thread().name, irrigateJob.valve.name, self.q.qsize()))
        if irrigateJob.valve.handled:
          self.logger.warning("Valve '%s' already handled. Returning to queue in 1 minute." % (irrigateJob.valve.name))
          time.sleep(61)
          self.q.put(irrigateJob)
        else:
          valve = irrigateJob.valve
          valve.handled = True
          
          # Link global waterflow sensor to this valve during operation
          if self.waterflow and self.waterflow.started:
            valve.waterflow = self.waterflow
          
          self.logger.info("Irrigation cycle start for valve '%s' for %s minutes." % (valve.name, irrigateJob.duration))
          duration = timedelta(minutes = irrigateJob.duration)
          valve.secondsLast = 0
          valve.litersLast = 0
          valve.secondsRemain = duration.seconds
          valve.secondsDuration = duration.seconds  # Store original duration for progress calculation
          initialOpen = valve.secondsDaily
          sensorDisabled = False
          openSince = None
          startTime = datetime.now()
          while startTime + duration > datetime.now():
            # The following two if statements needs to be together and first to prevent
            # the valve from opening if the sensor is disable.
            if irrigateJob.sensor is not None and irrigateJob.sensor.started:
              try:
                holdSensorDisabled = irrigateJob.sensor.shouldDisable()
                if holdSensorDisabled != sensorDisabled:
                  sensorDisabled = holdSensorDisabled
                  self.logger.info("Sensor disable set to '%s' for valve '%s'" % (sensorDisabled, valve.name))
                self.clearTempStatus("SensorErr")
                self.alerts.clear_alert_state(AlertType.SENSOR_ERROR, irrigateJob.sensor.name)
              except Exception as ex:
                self.setTempStatus("SensorErr")
                error_msg = format(ex)
                self.logger.error("Error probing sensor (shouldDisable) '%s': %s." % (irrigateJob.sensor.name, error_msg))
                self.alerts.alert(
                  AlertType.SENSOR_ERROR,
                  f"Sensor '{irrigateJob.sensor.name}' error: {error_msg}",
                  data={"sensor_name": irrigateJob.sensor.name, "error": error_msg}
                )
            
            # Detect manual close during job - valve closed but we still have openSince
            # Check this BEFORE trying to open to prevent re-opening after manual close
            if not valve.is_open and openSince is not None and not sensorDisabled:
              valve.secondsLast = (datetime.now() - openSince).seconds
              valve.secondsDaily = initialOpen + valve.secondsLast
              self.logger.info("Irrigation valve '%s' manually closed. Terminating job. Open time %s seconds." 
                              % (valve.name, valve.secondsLast))
              break
            
            if not valve.is_open and not sensorDisabled:
              valve.is_open = True
              openSince = datetime.now()
              valve.open()
              self.logger.info("Irrigation valve '%s' opened." % (valve.name))
            elif valve.is_open and openSince is None:
              # Valve already open (manually or previous job) - inherit it
              openSince = datetime.now()
              self.logger.info("Irrigation valve '%s' already open, job inheriting." % (valve.name))

            if valve.is_open and sensorDisabled:
              valve.is_open = False
              valve.secondsLast = (datetime.now() - openSince).seconds
              openSince = None
              valve.secondsDaily = initialOpen + valve.secondsLast
              initialOpen = valve.secondsDaily
              valve.secondsLast = 0
              valve.close()
              self.logger.info("Irrigation valve '%s' closed due to sensor." % (valve.name))
            
            if valve.is_open:
              if openSince is not None:
                valve.secondsLast = (datetime.now() - openSince).seconds
                valve.secondsDaily = initialOpen + valve.secondsLast
            if not valve.enabled:
              self.logger.info("Valve '%s' disabled. Terminating irrigation cycle." % (valve.name))
              break
            if self.terminated:
              self.logger.warning("Program exiting. Terminating irrigation cycle for valve '%s'..." % (valve.name))
              break

            valve.secondsRemain = ((startTime + duration) - datetime.now()).seconds
            self.logger.debug("Irrigation valve '%s' Last Open = %ss. Remaining = %ss. Daily Total = %ss." \
              % (valve.name, valve.secondsLast, valve.secondsRemain, valve.secondsDaily))
            time.sleep(1)
            if valve.waterflow is not None and valve.waterflow.started and self.everyXMinutes(valve.name, 1, False):
              _lastLiter_1m = valve.waterflow.lastLiter_1m()
              valve.litersDaily = valve.litersDaily + _lastLiter_1m
              valve.litersLast = valve.litersLast + _lastLiter_1m
              
              # Check for malfunction (no flow after 60 seconds)
              if valve.is_open and valve.secondsLast >= 60 and valve.litersLast == 0:
                self.alerts.alert(
                  AlertType.MALFUNCTION_NO_FLOW,
                  f"Valve '{valve.name}' open for {valve.secondsLast}s but no water flow detected",
                  valve_name=valve.name,
                  data={"seconds_open": valve.secondsLast, "liters_detected": valve.litersLast}
                )

          self.logger.info("Irrigation cycle ended for valve '%s'." % (valve.name))
          if valve.is_open:
            valve.secondsLast = (datetime.now() - openSince).seconds
            valve.secondsDaily = initialOpen + valve.secondsLast
          if valve.is_open:
            valve.is_open = False
            valve.close()
            self.logger.info("Irrigation valve '%s' closed. Overall open time %s seconds." % (valve.name, valve.secondsDaily))
          
          # Check for irregular flow at cycle end
          if valve.waterflow and valve.waterflow.started and valve.secondsLast > 0:
            self.checkIrregularFlow(valve, valve.secondsLast, valve.litersLast)
          
          # Clear malfunction state for next run
          self.alerts.clear_alert_state(AlertType.MALFUNCTION_NO_FLOW, valve.name)
          
          # Unlink waterflow sensor from valve
          valve.waterflow = None
          
          valve.handled = False
          self.telemetryValve(valve)
        self.q.task_done()
      except queue.Empty:
        pass
      except Exception as ex:
        self.logger.error("Error in valve handler thread '%s': %s" % (threading.current_thread().name, format(ex)))
        traceback.print_exc()
        # Ensure valve is left in a safe (closed) state
        try:
          if irrigateJob and irrigateJob.valve:
            if irrigateJob.valve.is_open:
              irrigateJob.valve.is_open = False
              irrigateJob.valve.close()
              self.logger.info("Safety-closed valve '%s' after error." % irrigateJob.valve.name)
            irrigateJob.valve.handled = False
            irrigateJob.valve.waterflow = None
        except Exception:
          pass
    self.logger.warning("Valve handler thread '%s' exited." % threading.current_thread().name)

  def queueJob(self, job):
    alive_workers = sum(1 for w in self.workers if w.is_alive())
    qsize = self.q.qsize()
    self.q.put(job)
    if job.sched is not None:
      self.logger.info(f"Valve '{job.valve.name}' job queued. Duration {job.duration} minutes. Queue size: {qsize + 1}. Worker threads alive: {alive_workers}/{len(self.workers)}.")
    else:
      self.logger.info(f"Valve '{job.valve.name}' adhoc job queued. Duration {job.duration} minutes. Queue size: {qsize + 1}. Worker threads alive: {alive_workers}/{len(self.workers)}.")

  def everyXMinutes(self, key, interval, bootstrap):
    if not key in self._intervalDict.keys():
      self._intervalDict[key] = datetime.now()
      return bootstrap

    if datetime.now() >= self._intervalDict[key] + timedelta(minutes=interval):
      self._intervalDict[key] = datetime.now()
      return True

    return False

  def timerThread(self):
    try:
      while True:
        tz = pytz.timezone(self.cfg.timezone)
        now = tz.localize(datetime.now().replace(second=0, microsecond=0))

        if now.hour == 0 and now.minute == 0:
          # Write yesterday's daily summaries before resetting
          yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
          write_daily_summaries(self.valves, yesterday, self.logger)
          
          # Reset daily counters
          for aValve in self.valves.values():
            aValve.secondsDaily = 0
            aValve.litersDaily = 0
          
          # Reload baselines with updated data
          load_baselines(self.valves, self.logger)

        if self.cfg.telemetry and self.everyXMinutes("idleInterval", self.cfg.telemIdleInterval, False):
          delta = (datetime.now() - self.startTime)
          uptime = ((delta.days * 86400) + delta.seconds) // 60
          self.mqtt.publish("/svc/uptime", uptime)
          
          for valve in self.valves.values():
            self.telemetryValve(valve)
          self.publishStatus()

          for sensor in self.sensors.keys():
            self.telemetrySensor(sensor, self.sensors[sensor])

        if self.cfg.telemetry and self.everyXMinutes("activeInterval", self.cfg.telemActiveInterval, False):
          for valve in self.valves.values():
            if valve.handled:
              self.telemetryValve(valve)

        if self.everyXMinutes("checkLeakInterval", 1, False):
          if self.waterflow is not None and self.waterflow.started and self.waterflow.leakdetection:
            all_closed = self.allValvesClosed()
            # Check for leak (unless in exclusion window)
            if all_closed:
              tz = pytz.timezone(self.cfg.timezone)
              now_tz = tz.localize(datetime.now())
              
              if not self.alerts.is_in_exclusion_window(now_tz):
                flow_rate = self.waterflow.lastLiter_1m()
                if flow_rate > 0:
                  self.alerts.alert(
                    AlertType.LEAK,
                    f"Leak detected: {flow_rate:.2f} L/min flow with all valves closed",
                    data={"flow_rate_lpm": flow_rate}
                  )
                  self.setTempStatus("Leaking")
                else:
                  # Leak resolved
                  self.alerts.clear_alert_state(AlertType.LEAK)
                  self.clearTempStatus("Leaking")
              else:
                # In exclusion window - clear any existing leak state
                self.alerts.clear_alert_state(AlertType.LEAK)
                self.clearTempStatus("Leaking")

        if self.everyXMinutes("scheduler", 1, True):
          # Must not evaluate more or less than once every minute otherwise running jobs will get queued again
          for aValve in self.valves.values():
            if aValve.enabled:
              if aValve.schedules is not None:
                for valveSched in aValve.schedules:
                  if self.evalSched(valveSched, self.cfg.timezone, now):
                    jobDuration = self.calculateJobDuration(aValve, valveSched)
                    job = model.Job(valve = aValve, duration = jobDuration, sched = valveSched)
                    self.queueJob(job)

        time.sleep(1)
    except Exception as ex:
      traceback.print_exc(ex)
      self.setStatus("Terminating")
      self.logger.error("Timer thread exited with error '%s'. Terminating Irrigate!" % format(ex))
      self.terminated = True
  
  def setTempStatus(self, tempStatus):
    self._tempStatus[tempStatus] = True
    self.publishStatus()

  def clearTempStatus(self, tempStatus):
    if tempStatus in self._tempStatus:
      del self._tempStatus[tempStatus]
    self.publishStatus()

  def setStatus(self, status):
    self._status = status
    self.publishStatus()

  def publishStatus(self):
    if len(self._tempStatus.keys()) > 0:
      self.mqtt.publish("/svc/status", ",".join(self._tempStatus.keys()))
    else:
      self.mqtt.publish("/svc/status", self._status)

  def allValvesClosed(self):
    for valve in self.valves.values():
      if valve.is_open:
        self._lastAllClosed = None
        return False

    # The waterflow sensor may still report some flow after the valve is closed (depends on the sensor
    # report interval, typically 10 seconds). So AllValvesClosed will report True only 60 seconds
    # after all valves have been closed.
    if self._lastAllClosed is None:
      self._lastAllClosed = datetime.now()

    return datetime.now() >= self._lastAllClosed + timedelta(0, 60)

  def telemetryValve(self, valve):
    statusStr = "enabled"
    if not valve.enabled:
      statusStr = "disabled"
    elif valve.is_open:
      statusStr = "open"

    if valve.is_open:
      self.mqtt.publish(valve.name+"/secondsLast", valve.secondsLast)
      if valve.waterflow is not None and valve.waterflow.started:
        self.mqtt.publish(valve.name+"/litersLast", valve.litersLast)
        if valve.secondsLast > 60 and valve.litersLast == 0:
          statusStr = "malfunction"

    self.mqtt.publish(valve.name+"/status", statusStr)
    self.mqtt.publish(valve.name+"/dailytotal", valve.secondsDaily)
    if valve.waterflow is not None and valve.waterflow.started:
      self.mqtt.publish(valve.name+"/dailyliters", valve.litersDaily)
    self.mqtt.publish(valve.name+"/remaining", valve.secondsRemain)

  def telemetrySensor(self, name, sensor):
    prefix = "sensor/" + name + "/"
    statusStr = "Enabled"
    try:
      if sensor.shouldDisable():
        statusStr = "Disabled"
      elif sensor.getFactor() != 1:
        statusStr = "Factored"
      self.mqtt.publish(prefix + "factor", sensor.getFactor())
      telem = sensor.getTelemetry()
      if telem is not None:
        for t in telem.keys():
          self.mqtt.publish(prefix + t, telem[t])
    except Exception as ex:
      statusStr = "Error"
    self.mqtt.publish(prefix + "status", statusStr)

  def getLogger(self):
    formatter = logging.Formatter(fmt='%(asctime)s %(levelname)-8s %(message)s',
                                  datefmt='%Y-%m-%d %H:%M:%S')
    handler = logging.FileHandler('log.txt', mode='w')
    handler.setFormatter(formatter)
    screen_handler = logging.StreamHandler(stream=sys.stdout)
    screen_handler.setFormatter(formatter)
    logger = logging.getLogger("MyLogger")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.addHandler(screen_handler)
    return logger

if __name__ == '__main__':
    sys.exit(main(sys.argv))
