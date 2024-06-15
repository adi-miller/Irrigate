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
# from flask_app import run_flask_app

def main(argv):
  options, remainder = getopt.getopt(sys.argv[1:], "", ["config=", "test"])

  configFilename = "config.json"
  test = False

  for opt, arg in options:
    if opt == "--config":
      configFilename = arg
    elif opt == "--test":
      test = True

  irrigate = Irrigate(configFilename)

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

  # flask_thread = threading.Thread(target=run_flask_app, args=(irrigate,))
  # flask_thread.daemon = True
  # flask_thread.start()
  
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
    self.init(configFilename)
    self.terminated = False
    self._lastAllClosed = None
    self.mqtt = Mqtt(self)
    self.createThreads()
    self._intervalDict = {}
    self._status = None
    self._tempStatus = {}

  def exit_gracefully(self, *args):
    self.terminated = True

  def start(self, test = True):
    if self.cfg.mqttEnabled:
      self.logger.info("Starting MQTT...")
      self.mqtt.start()

    self.logger.debug("Starting worker threads...")
    for worker in self.workers:
      self.logger.info("Starting worker thread '%s'." % worker.getName())
      worker.setDaemon(test)
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

    self.logger.info("Starting timer thread '%s'." % self.timer.getName())
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

  def createThreads(self):
    self.workers = []
    for i in range(self.cfg.valvesConcurrency):
      worker = Thread(target=self.valveThread, args=())
      worker.setDaemon(False)
      worker.setName("ValveTh%s" % i)
      self.workers.append(worker)

    self.timer = Thread(target=self.timerThread, args=())
    self.timer.setDaemon(True)
    self.timer.setName("TimerTh")

  def evalSched(self, sched, timezone, now):
    todayStr = calendar.day_abbr[datetime.today().weekday()]
    if len(sched.days) > 0 and todayStr not in sched.days:
      return False

    lat, lon = self.cfg.getLatLon()
    if len(sched.seasons) > 0 and self.getSeason(lat) not in sched.seasons:
      return False

    startTime = datetime.now()

    if sched.time_based_on == 'fixed':
      hours, minutes = sched.fixed_start_time.split(":")
      startTime = startTime.replace(hour=int(hours), minute=int(minutes), second=0, microsecond=0, tzinfo=pytz.timezone(timezone))
    else:
      sun = Sun(lat, lon)
      if sched.time_based_on == 'sunrise':
        startTime = sun.get_sunrise_time(time_zone=pytz.timezone(timezone)).replace(second=0, microsecond=0)
      elif sched.time_based_on == 'sunset':
        startTime = sun.get_sunset_time(time_zone=pytz.timezone(timezone)).replace(second=0, microsecond=0)
      startTime = startTime + timedelta(minutes=int(sched.offset_minutes))

    if startTime == now:
      return True

    return False

  def getSeason(self, lat):
    month = datetime.today().month
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

  def valveThread(self):
    while not self.terminated:
      try:
        irrigateJob = self.q.get(timeout=5)
        if irrigateJob.valve.handled:
          self.logger.warning("Valve '%s' already handled. Returning to queue in 1 minute." % (irrigateJob.valve.name))
          time.sleep(61)
          self.q.put(irrigateJob)
        else:
          valve = irrigateJob.valve
          valve.handled = True
          self.logger.info("Irrigation cycle start for valve '%s' for %s minutes." % (valve.name, irrigateJob.duration))
          duration = timedelta(minutes = irrigateJob.duration)
          valve.secondsLast = 0
          valve.litersLast = 0
          valve.secondsRemain = duration.seconds
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
                  self.logger.info("Suspend set to '%s' for valve '%s' from sensor" % (sensorDisabled, valve.name))
                self.clearTempStatus("SensorErr")
              except Exception as ex:
                self.setTempStatus("SensorErr")
                self.logger.error("Error probing sensor (shouldDisable) '%s': %s." % (irrigateJob.sensor.name, format(ex)))
            if not valve.is_open and not valve.suspended and not sensorDisabled:
              valve.is_open = True
              openSince = datetime.now()
              valve.open()
              self.logger.info("Irrigation valve '%s' opened." % (valve.name))

            if valve.is_open and (valve.suspended or sensorDisabled):
              valve.is_open = False
              valve.secondsLast = (datetime.now() - openSince).seconds
              openSince = None
              valve.secondsDaily = initialOpen + valve.secondsLast
              initialOpen = valve.secondsDaily
              valve.secondsLast = 0
              valve.close()
              self.logger.info("Irrigation valve '%s' closed." % (valve.name))
            if valve.is_open:
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

          self.logger.info("Irrigation cycle ended for valve '%s'." % (valve.name))
          if valve.is_open and not valve.suspended:
            valve.secondsLast = (datetime.now() - openSince).seconds
            valve.secondsDaily = initialOpen + valve.secondsLast
          if valve.is_open:
            valve.is_open = False
            valve.close()
            self.logger.info("Irrigation valve '%s' closed. Overall open time %s seconds." % (valve.name, valve.secondsDaily))
          valve.handled = False
          self.telemetryValve(valve)
        self.q.task_done()
      except queue.Empty:
        pass
    self.logger.warning("Valve handler thread '%s' exited." % threading.currentThread().getName())

  def queueJob(self, job):
    self.q.put(job)
    if job.sched is not None:
      self.logger.info(f"Valve '{job.valve.name}' job queued. Duration {job.duration} minutes.")
    else:
      self.logger.info(f"Valve '{job.valve.name}' adhoc job queued. Duration {job.duration} minutes.")

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
        now = datetime.now().replace(tzinfo=pytz.timezone(self.cfg.timezone), second=0, microsecond=0)

        if now.hour == 0 and now.minute == 0:
          for aValve in self.valves.values():
            aValve.secondsDaily = 0
            aValve.litersDaily = 0

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
            if self.allValvesClosed():
              if self.waterflow.lastLiter_1m() > 0:
                self.setTempStatus("Leaking")
              else:
                self.clearTempStatus("Leaking")

        if self.everyXMinutes("scheduler", 1, True):
          # Must not evaluate more or less than once every minute otherwise running jobs will get queued again
          for aValve in self.valves.values():
            if aValve.enabled:
              if aValve.schedules is not None:
                for valveSched in aValve.schedules:
                  if self.evalSched(valveSched, self.cfg.timezone, now):
                    jobDuration = valveSched.duration
                    if valveSched.enable_uv_adjustments:
                      try:
                        factor = self.uv_adjustments(aValve.sensor.getUv())
                        if factor != 1:
                          jobDuration *= factor
                          self.logger.info(f"Job duration changed from '{valveSched.duration}' to '{jobDuration}' based on input from sensor.")
                        self.clearTempStatus("SensorErr")
                      except Exception as ex:
                        self.setTempStatus("SensorErr")
                        self.logger.error("Error probing sensor (getUv) '%s': %s." % (valveSched.sensor.type, format(ex)))
                    job = model.Job(valve = aValve, duration = jobDuration, sched = valveSched)
                    self.queueJob(job)

        time.sleep(1)
    except Exception as ex:
      traceback.print_exc(ex)
      self.setStatus("Terminating")
      self.logger.error("Timer thread exited with error '%s'. Terminating Irrigate!" % format(ex))
      self.terminated = True
  
  def uv_adjustments(self, uv):
    for _ in self.cfg.cfg.uv_adjustments:
      if uv <= _.max_uv_index:
        return _.multiplier
      
    return self.cfg.cfg.uv_adjustments[-1].multiplier
  
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
    elif valve.suspended:
      statusStr = "suspended"
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
