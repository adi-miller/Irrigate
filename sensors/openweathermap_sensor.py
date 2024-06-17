import time
import requests
from threading import Thread
from datetime import timedelta
from datetime import datetime

from sensors.base_sensor import BaseSensor

class OpenWeatherMapSensor(BaseSensor):
  def __init__(self, logger, config):
    BaseSensor.__init__(self, logger, config)
    self.apiKey = config.api_key
    self.lat = config.latitude
    self.lon = config.longitude
    # self.updateInterval = config['updateinterval']
    # self.probabilityThreshold = config['probabilityThreshold']
    self._sendTelemetry = False

  def start(self):
    if self.started:
      return

    self.started = True
    self.logger.info("Sensor OpenWeatherMap starting...")
    self.worker = Thread(target=self.updaterThread, args=())
    self.worker.setDaemon(True)
    self.worker.setName("WeatTh")
    self.worker.start()

  def updaterThread(self):
    while True:
      self.logger.debug("Updating OpenWeatherMap data...")
      dateNow = datetime.now()
      # Get forecast
      url = f"https://api.openweathermap.org/data/3.0/onecall?exclude=current,minutely,hourly&units=metric&lat={self.lat}&lon={self.lon}&appid={self.apiKey}"
      res = self.call_api(url)
      if res is not None:
        self.uv = res['daily'][0]['uvi']
      self.logger.info(f"Daily UV Index ({dateNow.strftime('%c')}): {self.uv}")

      # Get recent
      self.recentPrecip = 0
      for i in range(3):
        day = dateNow - timedelta(i+1)
        url = f"https://api.openweathermap.org/data/3.0/onecall/day_summary?date={day.strftime('%Y-%m-%d')}&lat={self.lat}&lon={self.lon}&appid={self.apiKey}"
        res = self.call_api(url)
        if res is not None:
          self.recentPrecip += res["precipitation"]["total"]
      self.logger.info(f"Recent Precipitation: {self.recentPrecip}")
      self._sendTelemetry = True
      time.sleep(60*60*2)

  def call_api(self, url):
    self.logger.debug("Performing OpenWeatherMap HTTP request...")
    for retry in range(1, 4):
      try:
        response = requests.get(url)
        break
      except:
        self.logger.error(f"Error calling OpenWeatherMap... Attempt #{retry}...")
        time.sleep(2 * retry)
    else:
      self.logger.error("Failed calling OpenWeatherMap.")
      return None

    return response.json()
    
  def shouldDisable(self):
    # Disable if it rained recently
    if self.recentPrecip > 1:
      return True

    return False

  def getUv(self):
    return self.uv

  def getTelemetry(self):
    res = {}
    if self._sendTelemetry:
      res["uv"] = self.uv
      res["recentPrecip"] = self.recentPrecip
      self._sendTelemetry = False
    return res
