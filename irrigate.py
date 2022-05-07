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
import threading
from mqtt import Mqtt
from suntime import Sun
from datetime import datetime
from datetime import timedelta
from threading import Thread

def main(argv):
  options, remainder = getopt.getopt(sys.argv[1:], "", ["config=", "test"])

  configFilename = "config.yaml"
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
    self.sensors = {}
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
    for sch in self.cfg.schedules.values():
      if sch.sensor is not None:
        sensorHandler = sch.sensor.handler
        try:
          if not sensorHandler.started:
            self.logger.info("Starting sensor '%s'." % format(sensorHandler))
            sensorHandler.start()
            self.sensors[sch.sensor.type] = sensorHandler
        except Exception as ex:
          self.setStatus("InitErrSensor")
          self.logger.error("Error starting sensor '%s': '%s'." % (sch.sensor.type, format(ex)))

    self.logger.debug("Starting waterflows...")
    for waterflow in self.waterflows.values():
      if waterflow.handler is not None and waterflow.enabled:
        try:
          self.logger.info("Starting waterflow '%s'." % format(waterflow.handler))
          waterflow.handler.start()
        except Exception as ex:
          self.setStatus("InitErrWaterflow")
          self.logger.error("Error starting waterflow '%s': '%s'." % (waterflow.name, format(ex)))

    self.logger.info("Starting timer thread '%s'." % self.timer.getName())
    self.timer.start()

    if self._status is None:
      self.setStatus("OK")

  def init(self, cfgFilename):
    self.cfg = config.Config(self.logger, cfgFilename)
    self.valves = self.cfg.valves
    self.globalWaterflow = self.cfg.globalWaterflow
    self.waterflows = self.cfg.waterflows
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
    if todayStr not in sched.days:
      return False

    lat, lon = self.cfg.getLatLon()
    if sched.seasons is not None and not self.getSeason(lat) in sched.seasons:
      return False

    hours, minutes = sched.start.split(":")
    startTime = datetime.now()

    if sched.type == 'absolute':
      startTime = startTime.replace(hour=int(hours), minute=int(minutes), second=0, microsecond=0, tzinfo=pytz.timezone(timezone))
    else:
      sun = Sun(lat, lon)
      if sched.type == 'sunrise':
        startTime = sun.get_local_sunrise_time().replace(second=0, microsecond=0, tzinfo=pytz.timezone(timezone))
      elif sched.type == 'sunset':
        startTime = sun.get_local_sunset_time().replace(second=0, microsecond=0, tzinfo=pytz.timezone(timezone))

    if hours[0] == '+':
      hours = hours[1:]
      startTime = startTime + timedelta(hours=int(hours), minutes=int(minutes))
    if hours[0] == '-':
      hours = hours[1:]
      startTime = startTime - timedelta(hours=int(hours), minutes=int(minutes))

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
            if irrigateJob.sensor is not None and irrigateJob.sensor.handler.started:
              try:
                holdSensorDisabled = irrigateJob.sensor.handler.shouldDisable()
                if holdSensorDisabled != sensorDisabled:
                  sensorDisabled = holdSensorDisabled
                  self.logger.info("Suspend set to '%s' for valve '%s' from sensor" % (sensorDisabled, valve.name))
                self.clearTempStatus("SensorErr")
              except Exception as ex:
                self.setTempStatus("SensorErr")
                self.logger.error("Error probing sensor (shouldDisable) '%s': %s." % (irrigateJob.sensor.type, format(ex)))
            if not valve.open and not valve.suspended and not sensorDisabled:
              valve.open = True
              openSince = datetime.now()
              valve.handler.open()
              self.logger.info("Irrigation valve '%s' opened." % (valve.name))

            if valve.open and (valve.suspended or sensorDisabled):
              valve.open = False
              valve.secondsLast = (datetime.now() - openSince).seconds
              openSince = None
              valve.secondsDaily = initialOpen + valve.secondsLast
              initialOpen = valve.secondsDaily
              valve.secondsLast = 0
              valve.handler.close()
              self.logger.info("Irrigation valve '%s' closed." % (valve.name))
            if valve.open:
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
            if valve.waterflow is not None and valve.waterflow.handler.started and self.everyXMinutes(valve.name, 1, False):
              _lastLiter_1m = valve.waterflow.handler.lastLiter_1m()
              valve.litersDaily = valve.litersDaily + _lastLiter_1m
              valve.litersLast = valve.litersLast + _lastLiter_1m

          self.logger.info("Irrigation cycle ended for valve '%s'." % (valve.name))
          if valve.open and not valve.suspended:
            valve.secondsLast = (datetime.now() - openSince).seconds
            valve.secondsDaily = initialOpen + valve.secondsLast
          if valve.open:
            valve.open = False
            valve.handler.close()
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
      self.logger.info("Valve '%s' job queued per sched '%s'. Duration %s minutes." % (job.valve.name, job.sched.name, job.duration))
    else:
      self.logger.info("Valve '%s' adhoc job queued. Duration %s minutes." % (job.valve.name, job.duration))

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

        if self.everyXMinutes("idleInterval", self.cfg.telemIdleInterval, False) and self.cfg.telemetry:
          delta = (datetime.now() - self.startTime)
          uptime = ((delta.days * 86400) + delta.seconds) // 60
          self.mqtt.publish("/svc/uptime", uptime)
          for valve in self.valves.values():
            self.telemetryValve(valve)
          self.publishStatus()

          for sensor in self.sensors.keys():
            self.telemetrySensor(sensor, self.sensors[sensor])

        if self.everyXMinutes("activeInterval", self.cfg.telemActiveInterval, False) and self.cfg.telemetry:
          for valve in self.valves.values():
            if valve.handled:
              self.telemetryValve(valve)

        if self.everyXMinutes("checkLeakInterval", 1, False):
          if self.globalWaterflow is not None and self.globalWaterflow.handler.started and self.globalWaterflow.leakDetection:
            if self.allValvesClosed():
              if self.globalWaterflow.handler.lastLiter_1m() > 0:
                self.setTempStatus("Leaking")
              else:
                self.clearTempStatus("Leaking")

        if self.everyXMinutes("scheduler", 1, True):
          # Must not evaluate more or less than once every minute otherwise running jobs will get queued again
          for aValve in self.valves.values():
            if aValve.enabled:
              if aValve.schedules is not None:
                for valveSched in aValve.schedules.values():
                  if self.evalSched(valveSched, self.cfg.timezone, now):
                    jobDuration = valveSched.duration
                    if valveSched.sensor is not None and valveSched.sensor.handler is not None and valveSched.sensor.handler.started:
                      try:
                        factor = valveSched.sensor.handler.getFactor()
                        if factor != 1:
                          jobDuration = jobDuration * factor
                          self.logger.info("Job duration changed from '%s' to '%s' based on input from sensor." % (valveSched.duration, jobDuration))
                        self.clearTempStatus("SensorErr")
                      except Exception as ex:
                        self.setTempStatus("SensorErr")
                        self.logger.error("Error probing sensor (getFactor) '%s': %s." % (valveSched.sensor.type, format(ex)))
                    job = model.Job(valve = aValve, duration = jobDuration, sched = valveSched, sensor = valveSched.sensor)
                    self.queueJob(job)

        time.sleep(1)
    except Exception as ex:
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
      if valve.open:
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
    elif valve.open:
      statusStr = "open"

    if valve.open:
      self.mqtt.publish(valve.name+"/secondsLast", valve.secondsLast)
      if valve.waterflow is not None and valve.waterflow.handler.started:
        self.mqtt.publish(valve.name+"/litersLast", valve.litersLast)
        if valve.secondsLast > 60 and valve.litersLast == 0:
          statusStr = "malfunction"

    self.mqtt.publish(valve.name+"/status", statusStr)
    self.mqtt.publish(valve.name+"/dailytotal", valve.secondsDaily)
    if valve.waterflow is not None and valve.waterflow.handler.started:
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
    # screen_handler.setFormatter(formatter)
    logger = logging.getLogger("MyLogger")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.addHandler(screen_handler)
    return logger

if __name__ == '__main__':
    sys.exit(main(sys.argv))
