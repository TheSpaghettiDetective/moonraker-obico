import logging
import time
from moonraker_obico.webcam_capture import capture_jpeg
_logger = logging.getLogger('obico.nozzlecam')

class NozzleCamConfig:
    def __init__(self, snapshot_url):
        self.snapshot_url = snapshot_url
        self.snapshot_ssl_validation = False

class NozzleCam:

    def __init__(self, app_model, server_conn):
        self.config = app_model.config
        self.server_conn = server_conn
        self.on_first_layer = False
        self.printer_id = app_model.linked_printer['id']
        self.nozzle_config = self.create_nozzlecam_config()

    def start(self):
        if self.nozzle_config is None:
            return
        while True:
            if self.on_first_layer == True:
                try:
                    self.send_nozzlecam_jpeg(capture_jpeg(self.nozzle_config))
                except Exception as e:
                    _logger.warning('Failed to capture jpeg - ' + str(e))
            time.sleep(0.2) #TODO how many photos do we want?

    def send_nozzlecam_jpeg(self, snapshot):
        try:
            files = {'pic': snapshot}
            self.server_conn.send_http_request('POST', '/ent/api/nozzle_cam/pic/', timeout=60, files=files, data={}, raise_exception=True, skip_debug_logging=True)
        except Exception as e:
            _logger.warning('Failed to post jpeg - ' + str(e))

    def notify_server_nozzlecam_complete(self):
        self.on_first_layer = False
        if self.nozzle_config is None:
            return
        try:
            data = {'nozzlecam_status': 'complete'}
            self.server_conn.send_http_request('POST', '/ent/api/nozzle_cam/first_layer_done/', timeout=60, files={}, data=data, raise_exception=True, skip_debug_logging=True)
        except Exception as e:
            _logger.warning('Failed to send images - ' + str(e))

    def create_nozzlecam_config(self):
        try:
            ext_info = self.server_conn.send_http_request('GET', f'/ent/api/printers/{self.printer_id}/ext/', timeout=60, files={}, data={}, raise_exception=True, skip_debug_logging=True)
            nozzle_url = ext_info.json()['ext'].get('nozzlecam_url', '')
            if nozzle_url is None or len(nozzle_url) == 0:
                return None
            else:
                return NozzleCamConfig(nozzle_url)
        except Exception as e:
            _logger.warning('Failed to build nozzle config - ' + str(e))
            return None
