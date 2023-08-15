import os
import logging
import time
from zipfile import ZipFile

_logger = logging.getLogger('obico.celestrius')

class Celestrius:

    def __init__(self, app_model, server_conn ):
        self.config = app_model.config
        self.server_conn = server_conn
        self.on_first_layer = False

        parent_directory = os.path.dirname(self.config._config_path)
        self.celestrius_imgs_dir_path = os.path.join(parent_directory, 'celestrius_imgs')
        if not os.path.exists(self.celestrius_imgs_dir_path):
            os.makedirs(self.celestrius_imgs_dir_path)
        

    def start(self):
        while True:
            if self.on_first_layer == True:
                try:
                    # os.makedirs(self.celestrius_imgs_dir_path, exist_ok=True)
                    # replace webcam config with nozzle cam config
                    # create new catpure_celestris_jpeg func ?
                    # self.celestrius_images.append(capture_jpeg(self.config.webcam))
                    _logger.debug('Celestrius Jpeg captured')
                except Exception as e:
                    _logger.warn('Failed to capture jpeg - ' + str(e))
            time.sleep(0.2) # how many photos do we want?

    def dump_to_server(self):
        self.on_first_layer = False
        try:
            zip_file_path = self.create_zip()
            self.send_post_request(zip_file_path)
            self.remove_files_in_directory()
            _logger.debug('Celestrius images sent and files removed')
        except Exception as e:
            _logger.warn('Failed to send images - ' + str(e))
            self.remove_files_in_directory()
    
    def create_zip(self):
        zip_file_path = os.path.join(self.celestrius_imgs_dir_path, 'celestrius_images.zip')
        with ZipFile(zip_file_path, 'w') as zipf:
            for root, _, files in os.walk(self.celestrius_imgs_dir_path):
                for file in files:
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
        files = {'zip_file': open(zip_file_path, 'rb')}
        # response = requests.post(url, files=files) TODO use actual post request
        _logger.debug('POST request sent successfully')
