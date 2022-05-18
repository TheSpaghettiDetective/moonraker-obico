import logging
import logging.handlers
import sys

def setup_logging(logging_config):
    log_level_info = {'DEBUG': logging.DEBUG,
                      'INFO': logging.INFO,
                      'WARNING': logging.WARNING,
                      'ERROR': logging.ERROR,
	}
    logger = logging.getLogger('obico')
    log_level = log_level_info.get(logging_config.level.upper(), logging.INFO)
    logger.setLevel(log_level)

    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)8s  %(name)s - %(message)s"
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    if logging_config.path:
        fh = logging.handlers.RotatingFileHandler(
            logging_config.path, maxBytes=100000000, backupCount=5)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
