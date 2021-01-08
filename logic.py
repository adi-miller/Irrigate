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
  irrigate.start()
  try:
    while True:
      time.sleep(1)
  except KeyboardInterrupt:
    irrigate.logger.info("Program terminated. Waiting for all threads to finish...")
    irrigate.terminated = True

class Irrigate:
  def __init__(self, configFilename):
    self.init(configFilename)
    self.logger.info("Configuration '%s' loaded." % configFilename)
    self.terminated = False
    self.mqtt = Mqtt(self)
    if self.cfg.mqttEnabled:
      self.logger.info("MQTT initialzing...")
      self.mqtt.start()

    self.initThreads()

  def start(self, aAsync = True):
    self.logger.info("Starting scheduler thread '%s'." % self.sched.getName())
    self.sched.start()
    self.logger.info("Starting telemetry thread '%s'." % self.telemetry.getName())
    self.telemetry.start()

    if not aAsync:
      self.sched.join()
      self.logger.info("Scheduler thread exited. Terminating Irrigate!")

  def init(self, cfgFilename):
    self.logger = self.getLogger()
    self.cfg = config.Config(self.logger, cfgFilename)
    self.valves = self.cfg.valves
    self.q = queue.Queue()

  def initThreads(self):
    for i in range(self.cfg.valvesConcurrency):
      worker = Thread(target=self.irrigationHandler, args=())
      worker.setDaemon(False)
      worker.setName("ValveTh%s" % i)
      worker.start()

    self.sched = Thread(target=self.schedulerThread, args=())
    self.sched.setDaemon(True)
    self.sched.setName("SchedTh")

    if self.cfg.telemetry:
      self.telemetry = Thread(target=self.telemetryHander, args=())
      self.telemetry.setDaemon(True)
      self.telemetry.setName("TelemTh")

  def evalSched(self, sched, timezone):
    todayStr = calendar.day_name[datetime.today().weekday()]
    for day in sched.days:
      if todayStr.startswith(day):
        hours, minutes = sched.start.split(":")
        startTime = datetime.now()

        if sched.type == 'absolute':
          startTime = startTime.replace(hour=int(hours), minute=int(minutes), second=0, microsecond=0, tzinfo=pytz.timezone(timezone))
        else:
          lat, lon = self.cfg.getLatLon()
          sun = Sun(lat, lon)
          if sched.type == 'sunrise':
            startTime = sun.get_local_sunrise_time().replace(tzinfo=pytz.timezone(timezone))
          elif sched.type == 'sunset':
            startTime = sun.get_local_sunset_time().replace(tzinfo=pytz.timezone(timezone))

        if hours[0] == '+':
          hours = hours[1:]
          startTime = startTime + timedelta(hours=int(hours), minutes=int(minutes))
        if hours[0] == '-':
          hours = hours[1:]
          startTime = startTime - timedelta(hours=int(hours), minutes=int(minutes))

        now = datetime.now().replace(tzinfo=pytz.timezone(timezone), second=0, microsecond=0)
        if startTime == now:
          return True
        break
    return False

  def irrigationHandler(self):
    while not self.terminated:
      while self.q.empty() and not self.terminated:
        time.sleep(1)
      try:
        irrigateJob = self.q.get(timeout=1)
      except queue.Empty:
        continue
      if irrigateJob.valve.handled:
        self.logger.warning("Valve '%s' already handled. Returning to queue in 1 minute." % (irrigateJob.valve.name))
        time.sleep(61)
        self.q.put(irrigateJob)
      else:
        valve = irrigateJob.valve
        valve.handled = True
        self.logger.info("Irrigation cycle start for valve '%s' for %s minutes." % (valve.name, irrigateJob.duration))
        startTime = datetime.now()
        duration = timedelta(minutes = irrigateJob.duration)
        currentOpen = 0
        initialOpen = valve.openSeconds
        sensorDisabled = False
        openSince = None
        while startTime + duration > datetime.now():
          if self.terminated:
            self.logger.info("Program exiting. Terminating irrigation cycle for valve '%s'..." % (valve.name))
            break
          if irrigateJob.sched != None and irrigateJob.sched.sensor != None: 
            sensorDisabled = irrigateJob.sched.sensor.handler.shouldDisable()
          if valve.enabled == False:
            self.logger.info("Valve '%s' disabled. Terminating irrigation cycle." % (valve.name))
            break
          if not valve.open and not valve.suspended and not sensorDisabled:
            valve.open = True
            openSince = datetime.now()
            self.logger.info("Irrigation valve '%s' opened." % (valve.name))
          if valve.open and (valve.suspended or sensorDisabled):
            valve.open = False
            currentOpen = (datetime.now() - openSince).seconds
            openSince = None
            valve.openSeconds = initialOpen + currentOpen
            initialOpen = valve.openSeconds
            currentOpen = 0
            self.logger.info("Irrigation valve '%s' closed." % (valve.name))
          if valve.open:
            currentOpen = (datetime.now() - openSince).seconds
            valve.openSeconds = initialOpen + currentOpen

          self.logger.debug("Irrigation valve '%s' currentOpen = %s seconds. totalOpen = %s." % (valve.name, currentOpen, valve.openSeconds))
          time.sleep(1)

        self.logger.info("Irrigation cycle ended for valve '%s'." % (valve.name))
        if valve.open and not valve.suspended:
          currentOpen = (datetime.now() - openSince).seconds
          valve.openSeconds = initialOpen + currentOpen
        if valve.open:
          valve.open = False
          self.logger.info("Irrigation valve '%s' closed. Overall open time %s seconds." % (valve.name, valve.openSeconds))
        valve.handled = False
      self.q.task_done();

  def queueJob(self, job):
    exists = False
    for j in self.q.queue:
      if j.sched == job.sched and j.valve == job.valve:
        exists = True

    if not exists:
      self.q.put(job)
      if job.sched != None:
        self.logger.info("Valve '%s' job queued per sched '%s'. Duration %s minutes." % (job.valve.name, job.sched.name, job.duration))
      else:
        self.logger.info("Valve '%s' adhoc job queued. Duration %s minutes." % (job.valve.name, job.duration))

  def schedulerThread(self):
    while True:
      for aValve in self.valves.values():
        if aValve.enabled:
          if aValve.schedules != None:
            for valveSched in aValve.schedules.values():
              if self.evalSched(valveSched, self.cfg.timezone):
                job = model.Job(valve = aValve, sched = valveSched)
                self.queueJob(job)
      time.sleep(60)

  def telemetryHander(self):
    while True:
      self.mqtt.publish(self.cfg.mqttClientName + "/svc/uptime", 0)
      for valve in self.valves:
        statusStr = "enabled"
        if not self.cfg.valves[valve].enabled:
          statusStr = "disabled"
        elif self.cfg.valves[valve].suspended:
          statusStr = "suspended"
        elif self.cfg.valves[valve].open:
          statusStr = "open"
        if self.cfg.mqttEnabled:
          self.mqtt.publish(valve+"/status", statusStr)
          self.mqtt.publish(valve+"/duration", self.cfg.valves[valve].openSeconds)
          
      time.sleep(self.cfg.telemetryInterval * 60)

  def getLogger(self):
    formatter = logging.Formatter(fmt='%(asctime)s %(levelname)-8s %(message)s',
                                  datefmt='%Y-%m-%d %H:%M:%S')
    handler = logging.FileHandler('log.txt', mode='w')
    handler.setFormatter(formatter)
    screen_handler = logging.StreamHandler(stream=sys.stdout)
    screen_handler.setFormatter(formatter)
    logger = logging.getLogger("MyLogger")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.addHandler(screen_handler)
    return logger  

if __name__ == '__main__':
    sys.exit(main(sys.argv))

    