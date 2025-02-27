import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime

class TimestampedRotatingFileHandler(RotatingFileHandler):
    def __init__(self, filename, maxBytes=5*1024*1024, backupCount=20):
        os.makedirs("logs", exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename_with_timestamp = os.path.join("logs", f"{filename}.{timestamp}")
        super().__init__(filename_with_timestamp, maxBytes=maxBytes, backupCount=backupCount)
        
        
    def doRollover(self):
        # вызывается при превышении maxBytes
        super().doRollover()
        self._clean_old_files()

    def _clean_old_files(self):
        # удаление файлов превышающих backupCount
        files = sorted(
            [f for f in os.listdir() if f.startswith(self.baseFilename)],
            key=lambda x: os.path.getctime(x)
        )
        while len(files) > self.backupCount:
            oldest_file = files.pop(0)
            os.remove(oldest_file)