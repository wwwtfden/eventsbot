import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime

class TimestampedRotatingFileHandler(RotatingFileHandler):
    def __init__(self, filename, maxBytes=5*1024*1024, backupCount=20, encoding='utf-8'):
        os.makedirs("logs", exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename_with_timestamp = os.path.join("logs", f"{filename}.{timestamp}.log")
        super().__init__(
            filename=filename_with_timestamp,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding  # Явно указываем кодировку
        )
        self.namer = self.rotate_namer

    def rotate_namer(self, name):
        # Убираем временную метку из имени при ротации
        return name.replace(".log", "") + ".log"

    def _clean_old_files(self):
        files = sorted(
            [f for f in os.listdir("logs") if f.startswith(os.path.basename(self.baseFilename))],
            key=lambda x: os.path.getctime(os.path.join("logs", x))
        )
        while len(files) > self.backupCount:
            oldest_file = files.pop(0)
            os.remove(os.path.join("logs", oldest_file))
        
    def doRollover(self):
        # вызывается при превышении maxBytes
        super().doRollover()
        self._clean_old_files()

    # def _clean_old_files(self):
    #     # удаление файлов превышающих backupCount
    #     files = sorted(
    #         [f for f in os.listdir() if f.startswith(self.baseFilename)],
    #         key=lambda x: os.path.getctime(x)
    #     )
    #     while len(files) > self.backupCount:
    #         oldest_file = files.pop(0)
    #         os.remove(oldest_file)