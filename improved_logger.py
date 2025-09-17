import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime

class TimestampedRotatingFileHandler(RotatingFileHandler):
    def __init__(self, filename, maxBytes=5*1024*1024, backupCount=20, encoding='utf-8'):
        os.makedirs("logs", exist_ok=True)
        self.base_filename = filename
        self._update_filename()
        super().__init__(
            filename=self.filename,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=False
        )
        self.namer = self._namer
        self.rotator = self._rotator

    def _clean_old_files(self):
        files = sorted(
            [os.path.join("logs", f) for f in os.listdir("logs") if f.startswith("bot.log")],
            key=lambda x: os.path.getctime(x)
        )
        while len(files) > self.backupCount:
            oldest_file = files.pop(0)
            os.remove(oldest_file)

    def rotate_namer(self, name):
        # Убираем временную метку из имени при ротации
        return name.replace(".log", "") + ".log"
        
    def doRollover(self):
        # вызывается при превышении maxBytes
        super().doRollover()
        self._clean_old_files()

    def _update_filename(self):
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.filename = os.path.join("logs", f"{self.base_filename}.{timestamp}.log")

    def _namer(self, name):
        return name.replace(".log", "") + ".log"

    def _rotator(self, source, dest):
        os.rename(source, dest)

    def doRollover(self):
        self._update_filename()
        super().doRollover()

    def emit(self, record):
        self._update_filename()
        super().emit(record)
        self.flush()