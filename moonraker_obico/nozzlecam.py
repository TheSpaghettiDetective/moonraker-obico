import logging
import re
import time
from urllib.parse import urlparse
from moonraker_obico.webcam_capture import capture_jpeg
_logger = logging.getLogger('obico.nozzlecam')

class NozzleCamConfig:
    def __init__(self, snapshot_url):
        self.snapshot_url = snapshot_url
        self.snapshot_ssl_validation = False

class NozzleCam:

    def __init__(self, app_model, server_conn):
        self.model = app_model
        self.server_conn = server_conn
        self.on_first_layer = False
        self.printer_id = app_model.linked_printer['id']
        self.nozzle_config = self.create_nozzlecam_config()

    def start(self):
        if self.nozzle_config is None:
            return
        while True:
            if self.on_first_layer == True:
                if self.model.printer_state.is_printing():
                    try:
                        self.send_nozzlecam_jpeg(capture_jpeg(self.nozzle_config))
                    except Exception:
                        _logger.error('Failed to capture and send nozzle cam jpeg', exc_info=True)
                else:
                    self.notify_server_nozzlecam_complete() # edge case of single layer print or no 2nd layer to stop snapshots
            time.sleep(1)

    def send_nozzlecam_jpeg(self, snapshot):
        if snapshot:
                files = {'pic': snapshot}
                resp = self.server_conn.send_http_request('POST', '/ent/api/nozzle_cam/pic/', timeout=60, files=files, raise_exception=True, skip_debug_logging=True)
                _logger.debug('nozzle cam jpeg posted to server - {0}'.format(resp))

    def notify_server_nozzlecam_complete(self):
        self.on_first_layer = False
        if self.nozzle_config is None:
            return
        try:
            data = {'nozzlecam_status': 'complete'}
            self.server_conn.send_http_request('POST', '/ent/api/nozzle_cam/first_layer_done/', timeout=60, data=data, raise_exception=True, skip_debug_logging=True)
            _logger.debug('server notified 1st layer is done')
        except Exception:
            _logger.error('Failed to send images', exc_info=True)

    def create_nozzlecam_config(self):
        try:
            ext_info = self.server_conn.send_http_request('GET', f'/ent/api/printers/{self.printer_id}/ext/', timeout=60, raise_exception=True)
            nozzle_url = ext_info.json()['ext'].get('nozzlecam_url', '')
            if nozzle_url is None or len(nozzle_url) == 0:
                return None
            else:
                return NozzleCamConfig(nozzle_url)
        except Exception:
            _logger.error('Failed to build nozzle config', exc_info=True)
            return None
