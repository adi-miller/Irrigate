import sys
import time
import mqtt
import pytz
import model
import queue
import config
import logging
from datetime import datetime
from datetime import timedelta
import calendar
from threading import Thread
from suntime import Sun

def main(argv):
  logger, cfg, sun, valves, q = init("config.yaml")

  if cfg.mqttEnabled:
    mqtt.initMqtt(cfg, logger, q, model.queueJob)

  sched = initThreads(logger, cfg, sun, q, True)
  sched.join()

def initThreads(logger, cfg, sun, q, startSched):
  for i in range(cfg.valvesConcurrency):
    worker = Thread(target=irrigationHandler, args=(q, logger))
    worker.setDaemon(True)
    worker.setName("ValveTh%s" % i)
    worker.start()

  sched = Thread(target=schedulerThread, args=(q, cfg.valves, cfg, logger, sun))
  sched.setDaemon(True)
  sched.setName("SchedTh")
  if startSched:
    sched.start()

  return sched

def evalSched(sched, sun, timezone):
  todayStr = calendar.day_name[datetime.today().weekday()]
  for day in sched.days:
    if todayStr.startswith(day):
      hours, minutes = sched.start.split(":")
      startTime = datetime.now()

      if sched.type == 'absolute':
        startTime = startTime.replace(hour=int(hours), minute=int(minutes), second=0, microsecond=0, tzinfo=pytz.timezone(timezone))
      else:
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

def irrigationHandler(q, logger):
  while True:
    irrigateJob  = q.get()
    if irrigateJob.valve.handled:
      logger.warning("Valve '%s' already handled. Returning to queue in 1 minute." % (irrigateJob.valve.name))
      time.sleep(61)
      q.put(irrigateJob)
    else:
      valve = irrigateJob.valve
      valve.handled = True
      logger.info("Irrigation cycle start for valve '%s' for %s minutes." % (valve.name, irrigateJob.duration))
      startTime = datetime.now()
      duration = timedelta(minutes = irrigateJob.duration)
      currentOpen = 0
      initialOpen = valve.openSeconds
      sensorDisabled = False
      while startTime + duration > datetime.now():
        if irrigateJob.sched != None and irrigateJob.sched.sensor != None: 
          sensorDisabled = irrigateJob.sched.sensor.handler.shouldDisable()
        if valve.enabled == False:
          logger.info("Valve '%s' disabled. Terminating irrigation cycle." % (valve.name))
          break
        if not valve.open and not valve.suspended and not sensorDisabled:
          valve.open = True
          openSince = datetime.now()
          logger.info("Irrigation valve '%s' opened." % (valve.name))
        if valve.open and (valve.suspended or sensorDisabled):
          valve.open = False
          currentOpen = (datetime.now() - openSince).seconds
          openSince = None
          valve.openSeconds = initialOpen + currentOpen
          initialOpen = valve.openSeconds
          currentOpen = 0
          logger.info("Irrigation valve '%s' closed." % (valve.name))
        if valve.open:
          currentOpen = (datetime.now() - openSince).seconds
          valve.openSeconds = initialOpen + currentOpen

        logger.debug("Irrigation valve '%s' currentOpen = %s seconds. totalOpen = %s." % (valve.name, currentOpen, valve.openSeconds))
        time.sleep(1)

      logger.info("Irrigation cycle ended for valve '%s'." % (valve.name))
      if valve.open and not valve.suspended:
        currentOpen = (datetime.now() - openSince).seconds
        valve.openSeconds = initialOpen + currentOpen
      if valve.open:
        valve.open = False
        logger.info("Irrigation valve '%s' closed. Overall open time %s seconds." % (valve.name, valve.openSeconds))
      valve.handled = False
    q.task_done();

def schedulerThread(q, valves, cfg, logger, sun):
  while True:
    for aValve in valves.values():
      if aValve.enabled:
        if aValve.schedules != None:
          for valveSched in aValve.schedules.values():
            if evalSched(valveSched, sun, cfg.timezone):
              job = model.Job(valve = aValve, sched = valveSched)
              model.queueJob(logger, q, job)
            
    time.sleep(60)

def init(cfgFilename):
  logger = getLogger()
  cfg = config.Config(logger, cfgFilename)
  lat, lon = cfg.getLatLon()
  sun = initSun(lat, lon)
  valves = cfg.valves
  q = queue.Queue()
  return logger, cfg, sun, valves, q

def initSun(lat, lon):
  return Sun(lat, lon)

def getLogger():
  formatter = logging.Formatter(fmt='%(asctime)s %(levelname)-8s %(message)s',
                                datefmt='%Y-%m-%d %H:%M:%S')
  handler = logging.FileHandler('log.txt', mode='w')
  handler.setFormatter(formatter)
  screen_handler = logging.StreamHandler(stream=sys.stdout)
  screen_handler.setFormatter(formatter)
  logger = logging.getLogger("MyLogger")
  logger.setLevel(logging.DEBUG)
  # logger.addHandler(handler)
  logger.addHandler(screen_handler)
  return logger  

if __name__ == '__main__':
    sys.exit(main(sys.argv))


    