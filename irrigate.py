import sys
import time
import pytz
import model
import queue
import config
import logging
import calendar
from mqtt import Mqtt
from suntime import Sun
from datetime import datetime
from datetime import timedelta
from threading import Thread

def main(argv):
  configFilename = "config.yaml"
  if len(argv) > 1:
    configFilename = argv[1]
  irrigate = Irrigate(configFilename)
  irrigate.start(False)
  try:
    while True:
      time.sleep(1)
  except KeyboardInterrupt:
    irrigate.logger.info("Program terminated. Waiting for all threads to finish...")
    irrigate.terminated = True

class Irrigate:
  def __init__(self, configFilename):
    self.startTime = datetime.now()
    self.logger = self.getLogger()
    self.logger.info("Reading configuration file '%s'..." % configFilename)
    self.init(configFilename)
    self.terminated = False
    self.mqtt = Mqtt(self)
    self.createThreads()
    self._intervalDict = {}

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
      if sch.sensor != None:
        sensorHandler = sch.sensor.handler
        try:
          if not sensorHandler.started:
            self.logger.info("Starting sensor '%s'." % format(sensorHandler))
            sensorHandler.start()
        except Exception as ex:
          self.logger.error("Error starting sensor '%s': '%s'." % (sch.sensor.type, format(ex)))

    self.logger.info("Starting timer thread '%s'." % self.timer.getName())
    self.timer.start()

  def init(self, cfgFilename):
    self.cfg = config.Config(self.logger, cfgFilename)
    self.valves = self.cfg.valves
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
    if not todayStr in sched.days:
      return False
      
    lat, lon = self.cfg.getLatLon()
    if sched.seasons != None and not self.getSeason(lat) in sched.seasons:
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
          initialOpen = valve.secondsDaily
          sensorDisabled = False
          openSince = None
          startTime = datetime.now()
          while startTime + duration > datetime.now():
            # The following two if statements needs to be together and first to prevent
            # the valve from opening if the sensor is disable. 
            if irrigateJob.sensor != None and irrigateJob.sensor.handler.started: 
              try:
                holdSensorDisabled = irrigateJob.sensor.handler.shouldDisable()
                if holdSensorDisabled != sensorDisabled:
                  sensorDisabled = holdSensorDisabled
                  self.logger.info("Suspend set to '%s' for valve '%s' from sensor" % (sensorDisabled, valve.name))
              except Exception as ex:
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
            if valve.enabled == False:
              self.logger.info("Valve '%s' disabled. Terminating irrigation cycle." % (valve.name))
              break
            if self.terminated:
              self.logger.warning("Program exiting. Terminating irrigation cycle for valve '%s'..." % (valve.name))
              break

            valve.secondsRemain = ((startTime + duration) - datetime.now()).seconds
            self.logger.debug("Irrigation valve '%s' Last Open = %ss. Remaining = %ss. Daily Total = %ss." \
              % (valve.name, valve.secondsLast, valve.secondsRemain, valve.secondsDaily))
            time.sleep(1)

          self.logger.info("Irrigation cycle ended for valve '%s'." % (valve.name))
          if valve.open and not valve.suspended:
            valve.secondsLast = (datetime.now() - openSince).seconds
            valve.secondsDaily = initialOpen + valve.secondsLast
          if valve.open:
            valve.open = False
            valve.handler.close()
            self.logger.info("Irrigation valve '%s' closed. Overall open time %s seconds." % (valve.name, valve.secondsDaily))
          valve.handled = False
        self.q.task_done();
      except queue.Empty:
        pass

  def queueJob(self, job):
    self.q.put(job)
    if job.sched != None:
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

        if self.everyXMinutes("idleInterval", self.cfg.telemIdleInterval, False) and self.cfg.telemetry:
          uptime = (datetime.now() - self.startTime).seconds // 60
          self.mqtt.publish("/svc/uptime", uptime)
          for valve in self.valves.values():
            self.telemetryValve(valve)

        if self.everyXMinutes("activeinterval", self.cfg.telemActiveInterval, False) and self.cfg.telemetry:
          for valve in self.valves.values():
            if valve.handled:
              self.telemetryValve(valve)

        if self.everyXMinutes("scheduler", 1, True):
          # Must not evaluate more or less than once every minute otherwise running jobs will get queued again
          for aValve in self.valves.values():
            if aValve.enabled:
              if aValve.schedules != None:
                for valveSched in aValve.schedules.values():
                  if self.evalSched(valveSched, self.cfg.timezone, now):
                    jobDuration = valveSched.duration
                    if valveSched.sensor != None and valveSched.sensor.handler != None and valveSched.sensor.handler.started:
                      try:
                        factor = valveSched.sensor.handler.getFactor()
                        if factor != 1:
                          jobDuration = jobDuration * factor
                          self.logger.info("Job duration changed from '%s' to '%s' based on input from sensor." % (valveSched.duration, jobDuration))
                      except Exception as ex:
                        self.logger.error("Error probing sensor (getFactor) '%s': %s." % (valveSched.sensor.type, format(ex)))
                    job = model.Job(valve = aValve, duration = jobDuration, sched = valveSched, sensor = valveSched.sensor)
                    self.queueJob(job)
  
        time.sleep(1)
    except Exception as ex:
      self.logger.error("Timer thread exited with error '%s'. Terminating Irrigate!" % format(ex))
      self.terminated = True

  def telemetryValve(self, valve):
    statusStr = "enabled"
    if not valve.enabled:
      statusStr = "disabled"
    elif valve.suspended:
      statusStr = "suspended"
    elif valve.open:
      statusStr = "open"
    self.mqtt.publish(valve.name+"/status", statusStr)
    self.mqtt.publish(valve.name+"/dailytotal", valve.secondsDaily)
    if valve.open:
      self.mqtt.publish(valve.name+"/secondsLast", valve.secondsLast)
      self.mqtt.publish(valve.name+"/remaining", valve.secondsRemain)

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

    