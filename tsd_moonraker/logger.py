import logging
import logging.handlers
import sys


def getLogger(name=''):
    _name = 'main' if not name else f'main.{name}'
    return logging.getLogger(_name)


def setup_logging(filename, level):
    logger = getLogger()
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)8s  %(name)s - %(message)s"
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(formatter)

    logger.addHandler(sh)

    fh = logging.handlers.RotatingFileHandler(
        filename, maxBytes=100000000, backupCount=5)
    fh.setFormatter(formatter)

    logger.addHandler(fh)
