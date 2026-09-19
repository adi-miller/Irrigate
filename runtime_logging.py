import logging
import queue
import threading


class CheckedFileHandler(logging.FileHandler):
  def handleError(self, record):
    raise RuntimeError("Application log file write failed")


class AsyncLogHandler(logging.Handler):
  def __init__(self, sinks, capacity=512):
    super().__init__()
    self.sinks = list(sinks)
    self.queue = queue.Queue(maxsize=capacity)
    self.stop_event = threading.Event()
    self.worker = None
    self.dropped = 0
    self.last_error = None
    self.overflow = False

  def emit(self, record):
    try:
      self.queue.put_nowait(record)
    except queue.Full:
      self.dropped += 1
      self.overflow = True

  def start(self):
    if self.worker is None:
      self.worker = threading.Thread(target=self._run, name="ApplicationLog", daemon=True)
      self.worker.start()

  def _run(self):
    while not self.stop_event.is_set() or not self.queue.empty():
      try:
        record = self.queue.get(timeout=0.1)
      except queue.Empty:
        continue
      try:
        for sink in self.sinks:
          sink.handle(record)
        self.last_error = None
      except Exception as error:
        self.last_error = "Logging unavailable (%s)" % type(error).__name__
      finally:
        self.queue.task_done()
        if self.queue.qsize() < self.queue.maxsize // 2:
          self.overflow = False

  def get_health(self):
    alive = bool(self.worker and self.worker.is_alive())
    available = alive and not self.overflow and self.last_error is None
    return {"enabled": True, "available": available, "worker_alive": alive,
            "dropped_records": self.dropped, "queue_depth": self.queue.qsize(),
            "reason": self.last_error or ("log queue overflow" if self.overflow else
                                          None if alive else "log worker stopped")}

  def shutdown(self, timeout=2):
    self.start()
    self.stop_event.set()
    self.worker.join(timeout)
    if self.worker.is_alive():
      self.last_error = "Log worker shutdown timed out"
      return False
    for sink in self.sinks:
      sink.close()
    return True
