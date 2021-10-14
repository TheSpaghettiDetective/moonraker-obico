import logging
import sys


def getLogger(name=''):
    _name = 'main' if not name else f'main.{name}'
    return logging.getLogger(_name)


def setup_logging(level):
    logger = getLogger()
    logger.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)8s  %(name)s - %(message)s"
    )

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(formatter)

    logger.addHandler(ch)
