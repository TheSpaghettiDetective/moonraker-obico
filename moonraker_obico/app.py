from __future__ import absolute_import
from typing import Optional, Dict, List, Tuple
from numbers import Number
import argparse
import dataclasses
import time
import logging
import threading
import queue
import json
import re
import signal
import backoff
import pathlib
import subprocess
import requests  # type: ignore

from moonraker_obico.nozzlecam import NozzleCam
from .version import VERSION
from .utils import SentryWrapper, run_in_thread
from .webcam_capture import JpegPoster
from .webcam_stream import WebcamStreamer
from .logger import setup_logging
from .printer import PrinterState
from .config import MoonrakerConfig, ServerConfig, Config
from .moonraker_conn import MoonrakerConn, Event
from .server_conn import ServerConn
from .tunnel import LocalTunnel
from .printer_discovery import PrinterDiscovery, StubMoonrakerConn
from .client_conn import ClientConn
from .passthru_targets import PassthruExecutor, FileDownloader, Printer, MoonrakerApi, FileOperations


_logger = logging.getLogger('obico.app')
_default_int_handler = None
_default_term_handler = None

ACKREF_EXPIRE_SECS = 300


class App(object):

    @dataclasses.dataclass
    class Model:
        config: Config
        remote_status: Dict
        linked_printer: Dict
        printer_state: PrinterState

    def __init__(self):
        self.shutdown = False
        self.model = None
        self.sentry = None
        self.server_conn = None
        self.client_conn = None
        self.moonrakerconn = None
        self.webcam_streamer = None
        self.jpeg_poster = None
        self.local_tunnel = None
        self.printer = None
        self.passthru_executor = None
        self.q = queue.Queue(maxsize=1000)
        self.nozzlecam = None

    def push_event(self, event):
        if self.shutdown:
            _logger.debug(f'is shutdown, dropping event {event}')
            return False

        try:
            self.q.put_nowait(event)
            return True
        except queue.Full:
            _logger.warning(f'event queue is full, dropping event {event}')
            return False

    @backoff.on_exception(backoff.expo, Exception, max_value=120)
    def wait_for_auth_token(self, config):
        while True:
            if config.server.auth_token:
                break

            _logger.warning('auth_token not configured. Retry after 2s')
            time.sleep(2)

        return ServerConn(config, None, None, None).get_linked_printer()

    def start(self, args):
        config = Config(args.config_path)
        config.load_from_config_file()
        self.sentry = SentryWrapper(config=config)
        setup_logging(config.logging, log_path=args.log_path, debug=args.debug)

        config_dict = {section: dict(config._config.items(section)) for section in config._config.sections()}
        _logger.info(f'moonraker-obico config:\n{config_dict}')

        if args.dev_mode:
            self.moonrakerconn = StubMoonrakerConn()
        else:
            self.moonrakerconn = MoonrakerConn(config, self.sentry, self.push_event,)
            # Blocking call. When continued, moonrakeconn is guaranteed to be properly configured. Also config object is updated with moonraker objects
            self.moonrakerconn.block_until_klippy_ready()
            self.moonrakerconn.add_remote_event_handler('relink_obico', self.relink_obico)

        if not config.server.auth_token:
            discovery = PrinterDiscovery(config, self.sentry, moonrakerconn=self.moonrakerconn)
            discovery.start_and_block()
            config.load_from_config_file() # PrinterDiscovery may or may not have succeeded. Reload from the file to make sure auth_token is loaded
            config.update_moonraker_objects(self.moonrakerconn) # Config internal objects refreshed from the config file, make sure they are updated again with moonraker states

        # Blocking call. When continued, server is guaranteed to be properly configured, self.model.linked_printer existed.
        linked_printer = self.wait_for_auth_token(config)
        _logger.info('Linked printer: {}'.format(linked_printer))

        self.model = App.Model(
            config=config,
            remote_status={'viewing': False, 'should_watch': False},
            linked_printer=linked_printer,
            printer_state=PrinterState(config),
        )

        _cfg = self.model.config._config
        _logger.debug(f'moonraker-obico configurations: { {section: dict(_cfg[section]) for section in _cfg.sections()} }')

        self.server_conn = ServerConn(self.model.config, self.model.printer_state, self.process_server_msg, self.sentry)
        self.jpeg_poster = JpegPoster(self.model, self.server_conn, self.sentry)
        self.printer = Printer(self.model, self.moonrakerconn, self.server_conn)
        self.client_conn = ClientConn()
        self.webcam_streamer = WebcamStreamer(self.server_conn, self.moonrakerconn, self.client_conn, self.model, self.sentry)
        self.passthru_executor = PassthruExecutor(dict(
                _printer = self.printer,   # The client would pass "_printer" instead of "printer" for historic reasons
                webcam_streamer = self.webcam_streamer,
                jpeg_poster = self.jpeg_poster,
                file_downloader = FileDownloader(self.model, self.moonrakerconn, self.server_conn, self.sentry),
                moonraker_api = MoonrakerApi(self.model, self.moonrakerconn, self.sentry),
                file_operations = FileOperations(self.model, self.moonrakerconn, self.sentry)
            ),
            self.server_conn,
            self.sentry)

        self.local_tunnel = LocalTunnel(
            tunnel_config=self.model.config.tunnel,
            on_http_response=self.server_conn.send_ws_msg_to_server,
            on_ws_message=self.server_conn.send_ws_msg_to_server,
            sentry=self.sentry)

        run_in_thread(self.server_conn.start)
        run_in_thread(self.webcam_streamer.start, self.model.config.webcams)

        while not (self.server_conn.ss and self.server_conn.ss.connected()):
            _logger.warning('Connections not ready. Trying again in 1s...')
            time.sleep(1)

        ### Anything happens after this point can assume both server and moonraker connections are ready

        self.model.printer_state.thermal_presets = self.moonrakerconn.find_all_thermal_presets()
        self.model.printer_state.installed_plugins = self.moonrakerconn.find_all_installed_plugins()

        self.moonrakerconn.set_macro_variables('OBICO_LINK_STATUS', is_linked=True)

        self.server_conn.post_status_update_to_server(with_settings=True) # We should have already collected all printer settings such as heaters and presets by this time

        self.nozzlecam = NozzleCam(self.model, self.server_conn, self.moonrakerconn)
        run_in_thread(self.nozzlecam.start)

        run_in_thread(self.jpeg_poster.pic_post_loop)
        run_in_thread(self.status_update_to_client_loop)

        even_loop_thread = run_in_thread(self.event_loop)

        try:
            # Save printer_id in the database so that the app can use it to send user to the correct tunnel authorization page
            self.moonrakerconn.api_post('server/database/item', namespace='obico', key='printer_id', value=self.model.linked_printer.get('id'))
            even_loop_thread.join()
        except Exception:
            self.sentry.captureException()

    def stop(self, cause=None):
        if cause:
            _logger.info(f'shutdown ({cause})')
        else:
            _logger.info('shutdown')

        self.shutdown = True
        if self.server_conn:
            self.server_conn.close()
        if self.moonrakerconn:
            self.moonrakerconn.close()
        if self.client_conn:
            self.client_conn.close()

    # TODO: This doesn't work as ffmpeg seems to mess with signals as well
    def interrupted(self, signum, frame):
        print('Cleaning up moonraker-obico service... Press Ctrl-C again to quit immediately')
        self.stop()

        global _default_int_handler, _default_term_handler

        if _default_int_handler:
            signal.signal(signal.SIGINT, _default_int_handler)
            _default_int_handler = None

        if _default_term_handler:
            signal.signal(signal.SIGTERM, _default_term_handler)
            _default_term_handler = None


    def event_loop(self):
        # processes app events
        # alters state of app
        while self.shutdown is False:
            try:
                event = self.q.get()
                self._process_event(event)
            except Exception:
                self.sentry.captureException(msg=f'error processing event {event}')

    def _process_event(self, event):
        if event.name == 'fatal_error':
            self.stop(cause=event.data.get('exc'))

        elif event.name == 'shutdown':
            self.stop()

        elif event.sender == 'moonrakerconn':
            self._on_moonrakerconn_event(event)

    def _on_moonrakerconn_event(self, event):
        if event.name == 'mr_disconnected':
            # clear app's klippy state to indicate the loss of connection to Moonraker
            self._received_klippy_update({"status": {},})

        elif event.name == 'message':
            if 'error' in event.data:
                _logger.warning(f'error response from moonraker, {event}')

            elif event.data.get('method', '') in ('notify_klippy_disconnected', 'notify_klippy_shutdown'):
                # Click "Restart Klipper" or "Firmware restart" (same result) -> notify_klippy_disconnected
                # Unplug printer USB cable -> notify_klippy_shutdown
                # clear app's klippy state to indicate the loss of connection to the printer
                self._received_klippy_update({"status": {},})

            elif event.data.get('result') == 'ok':
                # printer action response
                self.moonrakerconn.request_status_update()

            elif event.data.get('method', '') == 'notify_status_update':
                # something important has changed,
                # fetching full status
                self.moonrakerconn.request_status_update()

            elif event.data.get('method', '') == 'notify_history_changed':
                self.moonrakerconn.request_status_update()

            elif event.data.get('method', '') == 'notify_gcode_response':
                msg = (event.data.get('params') or [''])[0]
                if msg.startswith('!!'):  # It seems to an undocumented feature that some gcode errors that are critical for the users to know are received as notify_gcode_response with "!!"
                    self.server_conn.post_printer_event_to_server('Moonraker Error', msg, attach_snapshot=True)
                    self.server_conn.send_ws_msg_to_server({'passthru': {'terminal_feed': {'msg': msg,'_ts': time.time()}}})
                else:
                    readable_msg = msg.replace('// ', '')
                    self.server_conn.send_ws_msg_to_server({'passthru': {'terminal_feed': {'msg': readable_msg,'_ts': time.time()}}})

        elif event.name == 'status_update':
            # full state update from moonraker
            if 'result' in event.data:
                self._received_klippy_update(event.data['result'])
            else:
                _logger.warning(f'Missing "result" in event data: {event.data}')

    def set_current_print(self, printer_state):

        def find_current_print_ts():
            cur_job = self.moonrakerconn.find_most_recent_job()
            if cur_job:
                return int(cur_job.get('start_time', '0'))
            else:
                _logger.warning(f'Active job indicate in print_stats: {printer_state.status}, but not in job history: {cur_job}')
                return None

        printer_state.set_current_print_ts(find_current_print_ts())

        filename = printer_state.status.get('print_stats', {}).get('filename')
        file_metadata = self.moonrakerconn.api_get('server/files/metadata', raise_for_status=True, filename=filename)
        printer_state.current_file_metadata = file_metadata

        # So that Obico server can associate the current print with a gcodefile record in the DB
        printer_state.set_obico_g_code_file_id(self.find_obico_g_code_file_id(printer_state.status, file_metadata))

    def unset_current_print(self, printer_state):
        printer_state.set_current_print_ts(-1)
        printer_state.current_file_metadata = None

    def find_obico_g_code_file_id(self, cur_status, file_metadata):
        filename = cur_status.get('print_stats', {}).get('filename')
        basename = pathlib.Path(filename).name if filename else None  # filename in the response is actually the relative path
        g_code_data = dict(
            filename=basename,
            safe_filename=basename,
            num_bytes=file_metadata['size'],
            agent_signature='ts:{}'.format(file_metadata['modified']),
            url=filename
            )
        resp = self.server_conn.send_http_request('POST', '/api/v1/octo/g_code_files/', timeout=60, data=g_code_data, raise_exception=True)
        return resp.json()['id']


    def post_print_event(self, print_event):
        ts = self.model.printer_state.current_print_ts
        if ts == -1:
            raise Exception('current_print_ts is -1 on a print_event, which is not supposed to happen.')

        _logger.info(f'print event: {print_event} ({ts})')

        self.server_conn.post_status_update_to_server(print_event=print_event)


    def _received_klippy_update(self, data):
        printer_state = self.model.printer_state

        prev_status = printer_state.update_status(data['status'])

        prev_state = PrinterState.get_state_from_status(prev_status)
        cur_state = PrinterState.get_state_from_status(printer_state.status)

        if prev_state != cur_state:
            _logger.info(
                'detected state change: {} -> {}'.format(
                    prev_state, cur_state
                )
            )

        if cur_state == PrinterState.STATE_OFFLINE:
            printer_state.set_current_print_ts(None)  # Offline means actually printing status unknown. It may or may not be printing.
            self.server_conn.post_status_update_to_server()
            return

        if printer_state.current_print_ts is None:
            # This should cover all the edge cases when there is an active job, but current_print_ts is not set,
            # e.g., moonraker-obico is restarted in the middle of a print
            if printer_state.has_active_job():
                self.set_current_print(printer_state)
            else:
                self.unset_current_print(printer_state)

        if cur_state == PrinterState.STATE_PRINTING:
            if prev_state == PrinterState.STATE_PAUSED:
                self.post_print_event(PrinterState.EVENT_RESUMED)
                return
            if prev_state == PrinterState.STATE_OPERATIONAL:
                self.set_current_print(printer_state)
                self.post_print_event(PrinterState.EVENT_STARTED)
                return

        if cur_state == PrinterState.STATE_PAUSED and prev_state == PrinterState.STATE_PRINTING:
            self.post_print_event(PrinterState.EVENT_PAUSED)
            return

        if cur_state == PrinterState.STATE_OPERATIONAL and prev_state in PrinterState.ACTIVE_STATES:
                _state = data['status'].get('print_stats', {}).get('state')
                if _state == 'cancelled':
                    self.post_print_event(PrinterState.EVENT_CANCELLED)
                    # PrintFailed as well to be consistent with OctoPrint
                    time.sleep(0.5)
                    self.post_print_event(PrinterState.EVENT_FAILED)
                elif _state == 'complete':
                    self.post_print_event(PrinterState.EVENT_DONE)
                elif _state == 'error':
                    self.post_print_event(PrinterState.EVENT_FAILED)
                else:
                    # FIXME
                    _logger.warning(
                        f'unexpected state "{_state}", please report.')

                self.unset_current_print(printer_state)
                return

        self.server_conn.post_status_update_to_server(is_critical=False) # Nothing critical has changed. Can be skipped so that it won't bombard the server

    def process_server_msg(self, msg):
        if 'remote_status' in msg:
            self.model.remote_status.update(msg['remote_status'])
            if self.model.remote_status['viewing']:
                self.jpeg_poster.need_viewing_boost.set()

        if 'commands' in msg:
            _logger.debug(f'Received commands from server: {msg}')

            for command in msg['commands']:
                if command['cmd'] == 'pause':
                    self.printer.pause()
                if command['cmd'] == 'cancel':
                    self.printer.cancel()
                if command['cmd'] == 'resume':
                    self.printer.resume()

        if 'passthru' in msg:
            passthru_thread = threading.Thread(target=self.passthru_executor.run, args=(msg,))
            passthru_thread.is_daemon = True
            passthru_thread.start()

        if msg.get('janus') and self.webcam_streamer and self.webcam_streamer.janus:
            _logger.debug(f'Received janus from server: {msg}')
            self.webcam_streamer.janus.pass_to_janus(msg.get('janus'))

        if msg.get('http.tunnelv2') and self.local_tunnel:
            kwargs = msg.get('http.tunnelv2')
            tunnel_thread = threading.Thread(
                target=self.local_tunnel.send_http_to_local_v2,
                kwargs=kwargs)
            tunnel_thread.is_daemon = True
            tunnel_thread.start()

        if msg.get('ws.tunnel') and self.local_tunnel:
            kwargs = msg.get('ws.tunnel')
            kwargs['type_'] = kwargs.pop('type')
            self.local_tunnel.send_ws_to_local(**kwargs)

    def relink_obico(self, data):
        if self.model and self.model.config and self.model.config._config:
            self.model.config._config.remove_option('server', 'auth_token')
            self.model.config.write()
            subprocess.call(["systemctl", "restart", "moonraker-obico"])
        else:
            _logger.warning('Not linked or not connected to server. Ignoring re-linking request.')


    def status_update_to_client_loop(self):
        while self.shutdown is False:
            time.sleep(2)
            self.post_printer_status_to_client()

    def post_printer_status_to_client(self):
        self.client_conn.send_msg_to_client({'status': self.model.printer_state.to_status()})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-c', '--config', dest='config_path', required=True,
        help='Path to config file (cfg)'
    )
    parser.add_argument(
        '-l', '--log-file', dest='log_path', required=False,
        help='Path to log file'
    )
    parser.add_argument(
        '-d', '--debug', dest='debug', required=False,
        action='store_true', default=False,
        help='Enable debug logging'
    )
    parser.add_argument(
        '--dev-mode', dest='dev_mode', required=False,
        action='store_true', default=False,
        help='Dev mode. Use stub moonraker. For dev only (not for production).'
    )
    args = parser.parse_args()
    App().start(args)
