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
        if snapshot: #TODO update with new endpoint & data
            try:
                files = {'pic': snapshot}
                self.server_conn.send_http_request('POST', '/api/v1/octo/printer_events/', timeout=60, raise_exception=True, files=files, data=None)
            except Exception as e:
                _logger.warning('Failed to post jpeg - ' + str(e))

    def notify_server_celestrius_complete(self):
        self.on_first_layer = False
        try: #TODO update with new endpoint & data
            data = {'celestrius_status': 'complete'}
            self.server_conn.send_http_request('POST', '/api/v1/octo/printer_events/', timeout=60, raise_exception=True, files=None, data=data)
            _logger.debug('server notified celestrius is done')
        except Exception as e:
            _logger.warning('Failed to send images - ' + str(e))
