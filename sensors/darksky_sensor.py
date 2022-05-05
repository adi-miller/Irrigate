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
    self.probabilityThreshold = config['probabilityThreshold']
    self.started = False
    self._sendTelemetry = False

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
      dateNow = datetime.now()
      # Get forecast
      self.uv, self.precip, self.precipProbability = self.getDailyObj(int(time.mktime(dateNow.timetuple())))
      self.logger.debug("Daily precip (%s): %s. Probability: %s" % (dateNow.strftime("%c"), self.precip, self.precipProbability))

      # Get recent
      self.recentPrecip = 0
      for i in range(3):
        day = dateNow - timedelta(i+1)
        aTime = int(time.mktime(day.date().timetuple()))
        uv, precip, precipProbability = self.getDailyObj(aTime)
        self.logger.debug("Recent precip (%s): %s. Probability: %s" % (day.strftime("%c"), precip, precipProbability))
        if precipProbability >= self.probabilityThreshold:
          self.recentPrecip = self.recentPrecip + precip

      self.logger.info("Daily UV: %s. Daily Precip (%s): %s. Recent Precip: %s" % \
        (self.uv, dateNow.strftime("%c"), self.precip, self.recentPrecip))

      self._sendTelemetry = True
      time.sleep(self.updateInterval*60)

  def getDailyObj(self, aTime):
    urlPattern = "https://api.darksky.net/forecast/%s/%s,%s,%s?units=auto"
    url = urlPattern % (self.apiKey, self.lat, self.lon, aTime)
    urlLog = urlPattern % ("<redacted>", "<redacted>", "<redacted>", aTime)
    self.logger.debug("Performing Darksky HTTP request: '%s'" % urlLog)
    for retry in range(1, 4):
      try:
        response = requests.get(url)
        break
      except:
        self.logger.error("Error calling Darksky... Attempt #%s..." % retry)
        time.sleep(2 * retry)
    else:
      self.logger.error("Failed calling Darksky.")
      return 0, 0, 0

    try:
      daily = response.json()['daily']['data'][0]
      uv = daily['uvIndex']
      precip = daily['precipIntensityMax']
      precipProbability = daily['precipProbability']
      return uv, precip, precipProbability
    except Exception as ex:
      self.logger.error("Error parsing Darksky response: %s." % format(ex))
      return 0, 0, 0

  def shouldDisable(self):
    # Disable if it is likely to rain today
    if self.precip > 0.9 and self.precipProbability >= self.probabilityThreshold:
      return True

    # Disable if it rained recently
    if self.recentPrecip > 1:
      return True

    return False

  def getFactor(self):
    if self.uv > 8:
      return 1.5

    if self.uv <= 3:
      return 0.5

    if self.recentPrecip > 0.3:
      return 0.5

    return 1

  def getTelemetry(self):
    res = {}
    if self._sendTelemetry:
      res["uv"] = self.uv
      res["precip"] = self.precip
      res["recentPrecip"] = self.recentPrecip
      self._sendTelemetry = False
    return res
    