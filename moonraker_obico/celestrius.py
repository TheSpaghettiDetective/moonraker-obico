import logging
import time
from moonraker_obico.webcam_capture import capture_jpeg
_logger = logging.getLogger('obico.celestrius')

class Celestrius:

    def __init__(self, app_model, server_conn ):
        self.config = app_model.config
        self.server_conn = server_conn
        self.on_first_layer = False

    def start(self):
        #TODO block users with no nozzle cam config
        while True:
            if self.on_first_layer == True:
                try:
                    #TODO replace webcam config with nozzle cam config
                    self.send_celestrius_jpeg(capture_jpeg(self.config.webcam))
                    _logger.debug('Celestrius Jpeg captured & sent')
                except Exception as e:
                    _logger.warning('Failed to capture jpeg - ' + str(e))
            time.sleep(0.2) #TODO how many photos do we want?

    def send_celestrius_jpeg(self, snapshot):
        if snapshot:
            try:
                files = {'pic': snapshot}
                data = {'viewing_boost': 'true'} # do we want viewing boost or {} ?
                self.server_conn.send_http_request('POST', '/ent/api/nozzle_cam/pic/', timeout=60, files=files, data=data, raise_exception=True, skip_debug_logging=True)
            except Exception as e:
                _logger.warning('Failed to post jpeg - ' + str(e))

    def notify_server_celestrius_complete(self):
        self.on_first_layer = False
        try:
            data = {'celestrius_status': 'complete'}
            self.server_conn.send_http_request('POST', '/ent/api/nozzle_cam/first_layer_done/', timeout=60, raise_exception=True, files={}, data=data)
            _logger.debug('server notified celestrius is done')
        except Exception as e:
            _logger.warning('Failed to send images - ' + str(e))
