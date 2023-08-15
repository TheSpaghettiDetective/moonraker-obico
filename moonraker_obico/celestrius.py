import os
import logging
import time
from zipfile import ZipFile
from moonraker_obico.webcam_capture import capture_jpeg

_logger = logging.getLogger('obico.celestrius')

class Celestrius:

    def __init__(self, app_model, server_conn ):
        self.config = app_model.config
        self.server_conn = server_conn
        self.on_first_layer = False
        self.snapshot_count = 0 # index  / key attribute to give image a unique filename

        parent_directory = os.path.dirname(self.config._config_path)
        self.celestrius_imgs_dir_path = os.path.join(parent_directory, 'celestrius_imgs')
        if not os.path.exists(self.celestrius_imgs_dir_path):
            os.makedirs(self.celestrius_imgs_dir_path)
        

    def start(self):
        #TODO block users with no nozzle cam config
        while True:
            if self.on_first_layer == True:
                try:
                    #TODO replace webcam config with nozzle cam config
                    self.save_snapshot_as_jpeg(capture_jpeg(self.config.webcam))
                    _logger.debug('Celestrius Jpeg captured')
                except Exception as e:
                    _logger.warning('Failed to capture jpeg - ' + str(e))
            time.sleep(0.2) #TODO how many photos do we want?

    def save_snapshot_as_jpeg(self, snapshot):
        if snapshot:
            self.snapshot_count += 1
            image_path = os.path.join(self.celestrius_imgs_dir_path, f'celestrius_{self.snapshot_count}.jpg')
            with open(image_path, 'wb') as image_file:
                image_file.write(snapshot)

    def dump_to_server(self):
        self.on_first_layer = False
        try:
            zip_file_path = self.create_zip()
            self.send_post_request(zip_file_path)
            self.remove_files_in_directory()
            _logger.debug('Celestrius images sent and files removed')
        except Exception as e:
            _logger.warning('Failed to send images - ' + str(e))
            self.remove_files_in_directory()
    
    def create_zip(self):
        zip_file_path = os.path.join(self.celestrius_imgs_dir_path, 'celestrius_images.zip')
        with ZipFile(zip_file_path, 'w') as zipf:
            for root, _, files in os.walk(self.celestrius_imgs_dir_path):
                for file in files:
                    if not file.endswith('.zip'):
                        file_path = os.path.join(root, file)
                        zipf.write(file_path, os.path.relpath(file_path, self.celestrius_imgs_dir_path))
        return zip_file_path
    
    def remove_files_in_directory(self):
        for root, _, files in os.walk(self.celestrius_imgs_dir_path):
            for file in files:
                file_path = os.path.join(root, file)
                os.remove(file_path)
        _logger.debug('Files removed from directory')

    def send_post_request(self, zip_file_path):
        try:
            files = {'celestrius': open(zip_file_path, 'rb')}
            self.server_conn.send_http_request('POST', '/api/v1/octo/printer_events/', timeout=60, raise_exception=True, files=files, data=None)
            _logger.debug('POST request sent successfully')
        except Exception as e:
            _logger.warn('Failed to post zip file' + str(e))
