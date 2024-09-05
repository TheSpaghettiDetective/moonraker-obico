import logging
import collections
import requests
import os
import sys
import time
import threading
import io
import pathlib

from .utils import sanitize_filename
from .state_transition import call_func_with_state_transition

_logger = logging.getLogger('obico.passthru')

MAX_GCODE_DOWNLOAD_SECONDS = 10 * 60


class PassthruExecutor:

    def __init__(self, passthru_targets, server_conn, sentry):
        self.passthru_targets = passthru_targets
        self.server_conn = server_conn
        self.sentry = sentry

        self.seen_refs = collections.deque(maxlen=100)

    def run(self, passthru_msg):
        _logger.debug(f'Received passthru from server: {passthru_msg}')

        passthru = passthru_msg['passthru']
        ack_ref = passthru.get('ref')
        if ack_ref is not None:
            # same msg may arrive through both ws and datachannel
            if ack_ref in self.seen_refs:
                _logger.debug('Ignoring already processed passthru message')
                return
            self.seen_refs.append(ack_ref)

        error = None
        try:
            target = self.passthru_targets.get(passthru.get('target'))
            func = getattr(target, passthru['func'], None)
            ret_value, error = func(*(passthru.get("args", [])), **(passthru.get("kwargs", {})))
        except (AttributeError, TypeError):
            error = 'Request not supported. Please make sure moonraker-obico is updated to the latest version. If moonraker-obico is already up to date and you still see this error, please contact Obico support at support@obico.io'
        except Exception as e:
            error = str(e)
            self.sentry.captureException()

        if ack_ref is not None:
            if error:
                resp = {'ref': ack_ref, 'error': error}
            else:
                resp = {'ref': ack_ref, 'ret': ret_value}

            self.server_conn.send_ws_msg_to_server({'passthru': resp})


### Below are individual passthru target

class FileDownloader:

    def __init__(self, model, moonrakerconn, server_conn, sentry):
        self.model = model
        self.moonrakerconn = moonrakerconn
        self.server_conn = server_conn
        self.sentry = sentry

    def download(self, g_code_file) -> None:

        def _download_and_print():
            try:
                _logger.info(
                    f'downloading from {g_code_file["url"]}')

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
                raise


        if self.model.printer_state.is_busy():
            return None, 'Printer busy!'

        call_func_with_state_transition(self.server_conn, self.model.printer_state, self.model.printer_state.STATE_GCODE_DOWNLOADING, _download_and_print, MAX_GCODE_DOWNLOAD_SECONDS)

        return {'target_path': g_code_file['filename']}, None


class Printer:

    def __init__(self, model, moonrakerconn, server_conn):
        self.model = model
        self.moonrakerconn = moonrakerconn
        self.server_conn = server_conn

    def call_printer_api_with_state_transition(self, printer_action, transient_state, timeout=5*60):

        def _call_printer_api():
            resp_data = self.moonrakerconn.api_post(f'printer/print/{printer_action}', timeout=timeout)

        call_func_with_state_transition(self.server_conn, self.model.printer_state, transient_state, _call_printer_api, timeout=timeout)

    def resume(self):
        self.call_printer_api_with_state_transition('resume', self.model.printer_state.STATE_RESUMING)

    def cancel(self):
        self.call_printer_api_with_state_transition('cancel', self.model.printer_state.STATE_CANCELLING)

    def pause(self):
        self.call_printer_api_with_state_transition('pause', self.model.printer_state.STATE_PAUSING)

    def jog(self, axes_dict) -> None:
        if not self.moonrakerconn:
            return None, 'Printer is not connected!'

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
        return None, None

    def home(self, axes) -> None:
        if not self.moonrakerconn:
            return None, 'Printer is not connected!'

        self.moonrakerconn.request_home(axes=axes)
        return None, None

    def set_temperature(self, heater, target_temp) -> None:
        if not self.moonrakerconn:
            return None, 'Printer is not connected!'

        self.moonrakerconn.request_set_temperature(heater=heater, target_temp=target_temp)
        return None, None


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
            if not self.moonrakerconn:
                return None, 'Printer is not connected!'

            api_func = getattr(self.moonrakerconn, f'api_{verb.lower()}', None)

            ret_value = None
            error = None
            try:
                # Wrap requests.exceptions.RequestException in Exception, since it's one of the configured errors_to_ignore
                try:
                    ret_value = api_func(self.func, timeout=30, **kwargs)
                except requests.exceptions.RequestException as exc:
                    if (self.func == "printer/gcode/script"):
                        raise Exception(' "{}" - "{}"'.format(self.func, kwargs.get('script', '')[:5])) from exc # Take first 5 characters of the scrips to see if Sentry grouping will behave more friendly
                    elif self.func == "machine/device_power/devices" and verb == "get" and hasattr(exc, 'response') and exc.response is not None and exc.response.status_code == 404:
                            return {'devices': []}, None # User has no power devices configured. This handling is much easier than checking configfile for [power xxx] before making request
                    raise Exception(' "{}" - "{}" '.format(self.func, verb)) from exc
            except Exception as ex:
                error = 'Error in calling "{}" - "{}"'.format(self.func, verb)
                self.sentry.captureException()

            return ret_value, error

class FileOperations:
    def __init__(self, model, moonrakerconn, sentry ):
        self.model = model
        self.moonrakerconn = moonrakerconn
        self.sentry = sentry


    def check_filepath_and_agent_signature(self, filepath, server_signature):
        file_metadata = None

        try:
            file_metadata = self.moonrakerconn.api_get('server/files/metadata', raise_for_status=True, filename=filepath)
            filepath_signature = 'ts:{}'.format(file_metadata['modified'])
            return filepath_signature == server_signature # check if signatures match -> Boolean
        except Exception as e:
            return False # file has been deleted, moved, or renamed

    def start_printer_local_print(self, file_to_print):
        if not self.moonrakerconn:
            return None, 'Printer is not connected!'

        ret_value = None
        error = None
        filepath = file_to_print['url']
        file_is_not_modified = self.check_filepath_and_agent_signature(filepath, file_to_print['agent_signature'])

        if file_is_not_modified:
            ret_value = 'Success'
            self.moonrakerconn.api_post('printer/print/start', raise_for_status=True, filename=filepath)
            return ret_value, error
        else:
            error = 'File has been modified! Did you move, delete, or overwrite this file?'
            return ret_value, error
