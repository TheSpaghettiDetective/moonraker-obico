import logging
import logging.handlers
import sys

def setup_logging(logging_config):
    handlers = []
    log_level_info = {'DEBUG': logging.DEBUG,
                      'INFO': logging.INFO,
                      'WARNING': logging.WARNING,
                      'ERROR': logging.ERROR,
	}
    logger = logging.getLogger()
    log_level = log_level_info.get(logging_config.level.upper(), logging.INFO)
    logger.setLevel(log_level)

    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)8s  %(name)s - %(message)s"
    )


    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    handlers.append(sh)

    if logging_config.path:
        fh = logging.handlers.RotatingFileHandler(
            logging_config.path, maxBytes=10000000, backupCount=5)
        fh.setFormatter(formatter)
        handlers.append(fh)

    for hdlr in logger.handlers[:]:
        logger.removeHandler(hdlr)

    for hdlr in handlers:
        logger.addHandler(hdlr)
