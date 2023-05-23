import logging
import requests
import os
import sys
import time
import threading
import io
import pathlib

from .utils import sanitize_filename

_logger = logging.getLogger('obico.file_downloader')

class FileDownloader:

    def __init__(self, model, moonrakerconn, server_conn, sentry):
        self.model = model
        self.moonrakerconn = moonrakerconn
        self.server_conn = server_conn
        self.sentry = sentry

    def download(self, g_code_file) -> None:
        if self.model.printer_state.is_printing():
            return {'error': 'Printer busy!'}

        thread = threading.Thread(
            target=self._download_and_print,
            args=(g_code_file, )
        )
        thread.daemon = True
        thread.start()

        return {'target_path': g_code_file['filename']}


    def _download_and_print(self, g_code_file):

        try:
          _logger.info(
              f'downloading from {g_code_file["url"]}')

          self.model.printer_state.set_gcode_downloading_started(time.time())

          safe_filename = sanitize_filename(g_code_file['safe_filename'])
          r = requests.get(
              g_code_file['url'],
              allow_redirects=True,
              timeout=60 * 30
          )
          r.raise_for_status()

          _logger.info(f'uploading "{safe_filename}" to moonraker')
          resp_data = self.moonrakerconn.api_post(
              'server/files/upload',
              multipart_filename=safe_filename,
              multipart_fileobj=r.content,
              path=self.model.config.server.upload_dir,
          )
          _logger.debug(f'upload response: {resp_data}')

          filepath_on_mr = resp_data['item']['path']
          file_metadata = self.moonrakerconn.api_get('server/files/metadata', raise_for_status=True, filename=filepath_on_mr)
          basename = pathlib.Path(filepath_on_mr).name  # filename in the response is actually the relative path
          g_code_data = dict(
              safe_filename=basename,
              agent_signature='ts:{}'.format(file_metadata['modified'])
              )

          # PATCH /api/v1/octo/g_code_files/{}/ should be called before printer/print/start call so that the file can be properly matched to the server record at the moment of PrintStarted Event
          resp = self.server_conn.send_http_request('PATCH', '/api/v1/octo/g_code_files/{}/'.format(g_code_file['id']), timeout=60, data=g_code_data, raise_exception=True)
          _logger.info(f'uploading "{safe_filename}" finished.')

          resp_data = self.moonrakerconn.api_post('printer/print/start', filename=filepath_on_mr)
        except:
          self.sentry.captureException()
        finally:
          self.model.printer_state.set_gcode_downloading_started(None)


class Printer:

    def __init__(self, model, moonrakerconn):
        self.model = model
        self.moonrakerconn = moonrakerconn

    def jog(self, axes_dict) -> None:
        if not self.moonrakerconn:
            return {
                        'error': 'Printer is not connected!',
                    }

        gcode_move = self.model.printer_state.status['gcode_move']
        is_relative = not gcode_move['absolute_coordinates']
        has_z = 'z' in {axis.lower() for axis in axes_dict.keys()}
        feedrate = (
            self.model.config.server.feedrate_z
            if has_z
            else self.model.config.server.feedrate_xy
        )

        self.moonrakerconn.request_jog(
            axes_dict=axes_dict, is_relative=is_relative, feedrate=feedrate
        )

    def home(self, axes) -> None:
        if not self.moonrakerconn:
            return {
                        'error': 'Printer is not connected!',
                    }

        self.moonrakerconn.request_home(axes=axes)

    def set_temperature(self, heater, target_temp) -> None:
        if not self.moonrakerconn:
            return {
                        'error': 'Printer is not connected!',
                    }
        mr_heater = self.model.config.get_mapped_mr_heater_name(heater)
        self.moonrakerconn.request_set_temperature(heater=mr_heater, target_temp=target_temp)


class MoonrakerApi:

    def __init__(self, model, moonrakerconn, sentry):
        self.model = model
        self.moonrakerconn = moonrakerconn
        self.sentry = sentry

    def __getattr__(self, func):
        proxy = self.MoonrakerApiProxy(func, self.model, self.moonrakerconn, self.sentry)
        return proxy.call_api

    class MoonrakerApiProxy:

        def __init__(self, func, model, moonrakerconn, sentry):
            self.func = func
            self.model = model
            self.moonrakerconn = moonrakerconn
            self.sentry = sentry

        def call_api(self, verb='get', **kwargs):
            api_func = getattr(self.moonrakerconn, f'api_{verb.lower()}', None)

            try:
                # Wrap requests.exceptions.RequestException in Exception, since it's one of the configured errors_to_ignore
                try:
                    ret_value = api_func(self.func, **kwargs)
                except requests.exceptions.RequestException as exc:
                    raise Exception('Error in calling "{}" - "{}" - "{}"'.format(self.func, verb, kwargs)) from exc
            except Exception as ex:
                error = 'Error in calling "{}" - "{}" - "{}"'.format(self.func, verb, kwargs)
                self.sentry.captureException()

