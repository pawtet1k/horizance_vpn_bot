import logging
import os
from datetime import date

class MyLogger:
    _instance = None

    def __new__(cls, base_dir="my_logger"):
        if cls._instance is None:
            cls._instance = super(MyLogger, cls).__new__(cls)
            cls._instance._init(base_dir)
        return cls._instance

    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.today = date.today().strftime("%d-%m-%y")
        self.this_month = date.today().strftime("%m-%y")
        self.logger = self._setup_logger()

    def _setup_logger(self):
        logger = logging.getLogger()
        if not logger.handlers:
            logger.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s: %(message)s (line: %(lineno)s, %(filename)s)", 
            datefmt="%H:%M:%S %d-%b-%y"
        )

        def set_logger_level(level, path):
            os.makedirs(os.path.join(self.base_dir, path), exist_ok=True)
            file_path = os.path.join(
                self.base_dir,
                path,
                f"{self.today}_logger_{level.name.lower()}.log"
            )
            handler = logging.FileHandler(file_path)
            handler.setLevel(level)
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        set_logger_level(logging.DEBUG, os.path.join("debug", self.this_month))
        set_logger_level(logging.INFO, os.path.join("info", self.this_month))
        set_logger_level(logging.WARNING, "warning")
        set_logger_level(logging.ERROR, "")

        return logger

    def debug(self, message):
        self.logger.debug(message)

    def info(self, message):
        self.logger.info(message)

    def warning(self, message):
        self.logger.warning(message)

    def error(self, message):
        self.logger.error(message)
