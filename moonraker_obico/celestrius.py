import os
import logging
import time

_logger = logging.getLogger('obico.webcam_stream')

JANUS_SERVER = os.getenv('JANUS_SERVER', '127.0.0.1')
FFMPEG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'ffmpeg')
FFMPEG = os.path.join(FFMPEG_DIR, 'run.sh')


class Celestrius:

    def __init__(self, app_model, server_conn ):
        self.config = app_model.config
        self.server_conn = server_conn
        self.on_first_layer = False
        self.celestrius_img_path = ''


    def start(self):
        # create tmp directory if not present
        while True:
            if self.on_first_layer == True:
                try:
                    # replace webcam config with nozzle cam config
                    # create new catpure_celestris_jpeg func ?
                    # self.celestrius_images.append(capture_jpeg(self.config.webcam))
                    _logger.debug('Celestrius Jpeg captured')
                except Exception as e:
                    _logger.warn('Failed to capture jpeg - ' + str(e))
            time.sleep(0.2) # how many photos do we want?

    def dump_to_server(self):
        self.on_first_layer = False
        # zip tmp directory, store & clear tmp dir
        try:
            # replace below with new post call when server ready
            # self.plugin.server_conn.send_http_request('POST', '/api/v1/octo/pic/', timeout=60, files=self.celestrius_images, data=None, raise_exception=True, skip_debug_logging=True)
            _logger.debug('Celestrius images sent')
        except Exception as e:
            _logger.warn('Failed to send images - ' + str(e) )
