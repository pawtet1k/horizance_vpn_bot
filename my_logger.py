import logging
import os
import time
from datetime import date, timedelta, timezone


os.environ['TZ'] = 'Europe/Moscow'
time.tzset()
#moscow_tz = timezone(timedelta(hours=3))

def setup_logger(name='my_logger'):
    logger = logging.getLogger(name)

    if logger.handlers:
        logger.warning('Handlers already exit! Clearing them...')
        logger.handlers.clear()
    logger.setLevel(logging.DEBUG)

    def check_date(log_func, logger=logger):
        def wrapper(*args, **kwargs):
            current_date = date.today().strftime("%d-%m-%y")
            if current_date not in logger.handlers[0].baseFilename:
                logger.handlers.clear()
                setup_handlers(logger)
            return log_func(*args, **kwargs)
        return wrapper

    logger.debug = check_date(logger.debug)
    logger.info = check_date(logger.info)
    logger.warning = check_date(logger.warning)
    logger.error = check_date(logger.error)

    setup_handlers(logger)
    return logger

def setup_handlers(logger):
    # HORIZANCE_LOG_DIR задаёт test.py (временная папка): иначе тесты писали
    # в боевые логи — в них появлялись user 333001/777xxx и нода «DE 10.0.0.1»
    base = os.environ.get("HORIZANCE_LOG_DIR", logger.name)
    today = date.today().strftime("%d-%m-%y")
    this_month = date.today().strftime("%m-%y")
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s (line: %(lineno)s, %(filename)s)",
        datefmt="%H:%M:%S %d-%b-%y"
    )
    def set_logger_level(level, path, logger=logger, formatter=formatter):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        level_name = logging.getLevelName(level).lower()
        handler = logging.FileHandler(path)
        handler.setLevel(level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    set_logger_level(
        logging.DEBUG,f"{base}/debug/{this_month}/{today}_debug.log"
    )
    set_logger_level(
        logging.INFO, f"{base}/info/{this_month}/{today}_info.log"
    )
    set_logger_level(logging.WARNING,f"{base}/{this_month}_warning.log")
    set_logger_level(logging.ERROR, f"{base}/error.log")

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

logger = setup_logger()
