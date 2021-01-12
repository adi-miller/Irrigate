import time
import requests
from threading import Thread
from datetime import timedelta
from datetime import datetime

class DarkskySensor:
  def __init__(self, logger, config):
    self.logger = logger
    self.config = config
    self.apiKey = config['apikey']
    self.lat = config['latitude']
    self.lon = config['longitude']
    self.updateInterval = config['updateinterval']
    self.started = False

  def start(self):
    if self.started:
      return

    self.started = True
    self.logger.info("Sensor Darksky starting...")
    self.worker = Thread(target=self.updaterThread, args=())
    self.worker.setDaemon(True)
    self.worker.setName("DarkSTh")
    self.worker.start()

  def updaterThread(self):
    while True:
      self.logger.info("Updating Darkysky data...")
      # Get forecast
      self.uv, self.precip, self.precipProbability = self.getDailyObj(int(time.mktime(datetime.now().timetuple())))

      # Get recent
      self.precipRecently = 0
      for i in range(1, 3):
        day = (datetime.now() - timedelta(i)).date()
        aTime = int(time.mktime(day.timetuple()))
        uv, precip, precipProbability = self.getDailyObj(aTime)
        self.precipRecently = self.precipRecently + precip 

      self.logger.debug("Daily UV: %s. Daily Precip: %s. Precip probability: %s. Recent Precip: %s" % (self.uv, self.precip, self.precipProbability, self.precipRecently))
      time.sleep(self.updateInterval*60)

  def getDailyObj(self, aTime):
    url = "https://api.darksky.net/forecast/%s/%s,%s,%s?units=auto" % (self.apiKey, self.lat, self.lon, aTime)
    self.logger.debug("Performing Darksky HTTP request: '%s'" % url)
    for retry in range(1, 3):
      try:
        response = requests.get(url)
        break
      except:
        self.logger.error("Error calling Darksky in attempt #'%s'..." % retry)
        time.sleep(2*retry)
    else:
      self.logger.error("Failed calling Darksky! Sensor is not functioning.")
      return 0, 0, 0

    daily = response.json()['daily']['data'][0]
    uv = daily['uvIndex']
    precip = daily['precipIntensityMax']
    precipProbability = daily['precipProbability']

    return uv, precip, precipProbability

  def shouldDisable(self):
    # Disable if it is likely to rain today
    if self.precipProbability > 0.5 and self.precip > 0.1:
      return True

    # Disable if it rained recently 
    if self.precipRecently > 1:
      return True

    return False

  def getFactor(self):
    if self.uv > 8:
      return 1.5

    if self.uv <= 3:
      return 0.5

    if self.precipRecently > 0.3:
      return 0.5

    return 1
