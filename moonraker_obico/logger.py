import logging
import logging.handlers
import sys

def setup_logging(logging_config, log_path=None, debug=False):
    if log_path:
        logging_config.path = log_path
    if debug:
        logging_config.level = 'DEBUG'

    handlers = []
    log_level_info = {'DEBUG': logging.DEBUG,
                      'INFO': logging.INFO,
                      'WARNING': logging.WARNING,
                      'ERROR': logging.ERROR,
                      'CRITICAL': logging.CRITICAL,
	}

    logger = logging.getLogger()
    log_level = log_level_info.get(logging_config.level.upper(), logging.ERROR)
    logger.setLevel(log_level)

    if logging_config.log_network:
        logging.getLogger("urllib3").setLevel(log_level)
        logging.getLogger("backoff").setLevel(log_level)
    else:
        logging.getLogger("urllib3").setLevel(logging.CRITICAL) # So that we don't flood the logs with network errors
        logging.getLogger("backoff").setLevel(logging.CRITICAL) # So that we don't flood the logs with retry failures

    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)8s  %(name)s - %(message)s"
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    handlers.append(sh)

    if logging_config.path:
        fh = logging.handlers.RotatingFileHandler(
            logging_config.path, maxBytes=10000000, backupCount=2)
        fh.setFormatter(formatter)
        handlers.append(fh)

    for hdlr in logger.handlers[:]:
        logger.removeHandler(hdlr)

    for hdlr in handlers:
        logger.addHandler(hdlr)
