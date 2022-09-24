from __future__ import absolute_import
from typing import Optional, Dict, List, Tuple
from numbers import Number
import argparse
import dataclasses
import time
import logging
import threading
import collections
import queue
import json
import re
import signal
import backoff

import requests  # type: ignore

from .version import VERSION
from .utils import get_tags, sanitize_filename
from .webcam_capture import JpegPoster
from .logger import setup_logging
from .printer import PrinterState
from .config import MoonrakerConfig, ServerConfig, Config
from .moonraker_conn import MoonrakerConn, Event
from .server_conn import ServerConn
from .webcam_stream import WebcamStreamer
from .janus import JanusConn


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
        seen_refs: collections.deque
        downloading_gcode_file: bool = False

        def is_configured(self):
            return True  # FIXME

    def __init__(self):
        self.shutdown = False
        self.model = None
        self.sentry = None
        self.server_conn = None
        self.moonrakerconn = None
        self.webcam_streamer = None
        self.jpeg_poster = None
        self.janus = None
        self.q: queue.Queue = queue.Queue(maxsize=1000)

    def push_event(self, event):
        if self.shutdown:
            _logger.debug(f'is shutdown, dropping event {event}')
            return False

        try:
            self.q.put_nowait(event)
            return True
        except queue.Full:
            _logger.error(f'event queue is full, dropping event {event}')
            return False

    @backoff.on_exception(backoff.expo, Exception, max_value=60)
    def wait_for_auth_token(self, args):
        while True:
            config = Config(args.config_path)
            if args.log_path:
                config.logging.path = args.log_path
            if args.debug:
                config.logging.level = 'DEBUG'
            setup_logging(config.logging)

            if config.server.auth_token:
                _logger.info('Fetching linked printer...')
                linked_printer = ServerConn(config, None, None, None).get_linked_printer()


                _logger.info(f'starting moonraker-obico (v{VERSION})')
                _logger.info('Linked printer: {}'.format(linked_printer))

                self.model = App.Model(
                    config=config,
                    remote_status={'viewing': False, 'should_watch': False},
                    linked_printer=linked_printer,
                    printer_state=PrinterState(config),
                    seen_refs=collections.deque(maxlen=100),
                )
                self.sentry = self.model.config.get_sentry()
                break

            _logger.warning('auth_token not configured. Retry after 2s')
            time.sleep(2)

    def start(self, args):
        # TODO: This doesn't work as ffmpeg seems to mess with signals as well
        # global _default_int_handler, _default_term_handler
        # _default_int_handler = signal.signal(signal.SIGINT, self.interrupted)
        # _default_term_handler = signal.signal(signal.SIGTERM, self.interrupted)

        # Blocking call. When continued, server is guaranteed to be properly configured, self.model.linked_printer existed.
        self.wait_for_auth_token(args)
        get_tags()

        _cfg = self.model.config._config
        _logger.debug(f'moonraker-obico configurations: { {section: dict(_cfg[section]) for section in _cfg.sections()} }')
        self.server_conn = ServerConn(self.model.config, self.model.printer_state, self.process_server_msg, self.sentry, )
        self.moonrakerconn = MoonrakerConn(self.model.config, self.sentry, self.push_event,)
        self.janus = JanusConn(self.model.config, self.server_conn, self.sentry)
        self.jpeg_poster = JpegPoster(self.model, self.server_conn, self.sentry)

        self.moonrakerconn.update_webcam_config_from_moonraker()

        if not self.model.config.webcam.disable_video_streaming:
            _logger.info('Starting webcam streamer')
            self.webcam_streamer = WebcamStreamer(self.model, self.server_conn, self.sentry)
            stream_thread = threading.Thread(target=self.webcam_streamer.video_pipeline)
            stream_thread.daemon = True
            stream_thread.start()

        thread = threading.Thread(target=self.server_conn.start)
        thread.daemon = True
        thread.start()

        thread = threading.Thread(target=self.moonrakerconn.start)
        thread.daemon = True
        thread.start()

        jpeg_post_thread = threading.Thread(target=self.jpeg_poster.pic_post_loop)
        jpeg_post_thread.daemon = True
        jpeg_post_thread.start()

        thread = threading.Thread(target=self.event_loop)
        thread.daemon = True
        thread.start()

        # Janus may take a while to start, or fail to start. Put it in thread to make sure it does not block
        janus_thread = threading.Thread(target=self.janus.start)
        janus_thread.daemon = True
        janus_thread.start()

        try:
            thread.join()
        except Exception:
            self.sentry.captureException()

    def stop(self, cause=None):
        if cause:
            _logger.error(f'shutdown ({cause})')
        else:
            _logger.info('shutdown')

        self.shutdown = True
        if self.server_conn:
            self.server_conn.close()
        if self.moonrakerconn:
            self.moonrakerconn.close()
        if self.janus:
            self.janus.shutdown()

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
                if msg.startswith('!!'):  # It seems to an undocumented feature that some gcode errors that are critical for the users to know are recevied as notify_gcode_response with "!!"
                    self.server_conn.post_printer_event_to_server('Moonraker Error', msg, attach_snapshot=True)

        elif event.name == 'status_update':
            # full state update from moonraker
            self._received_klippy_update(event.data['result'])


    def _download_and_print(self, gcode_file):
        filename = gcode_file['filename']

        _logger.info(
            f'downloading "{filename}" from {gcode_file["url"]}')

        safe_filename = sanitize_filename(filename)
        path = self.model.config.server.upload_dir

        r = requests.get(
            gcode_file['url'],
            allow_redirects=True,
            timeout=60 * 30
        )
        r.raise_for_status()

        self.model.downloading_gcode_file = False

        _logger.info(f'uploading "{filename}" to moonraker')
        resp_data = self.moonrakerconn.api_post(
                'server/files/upload',
                filename=filename,
                fileobj=r.content,
                path=path,
                print='true',
        )

        _logger.debug(f'upload response: {resp_data}')
        _logger.info(
            f'uploading "{filename}" finished.')


    def find_current_print_ts(self, cur_status):
        cur_job = self.moonrakerconn.find_most_recent_job()
        if cur_job:
            return int(cur_job.get('start_time', '0'))
        else:
            _logger.error(f'Active job indicate in print_stats: {cur_status}, but not in job history: {cur_job}')
            return None

    def post_print_event(self, print_event):
        ts = self.model.printer_state.current_print_ts
        if ts == -1:
            _logger.error('current_print_ts is -1 on a print_event, which is not supposed to happen.')

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
                printer_state.set_current_print_ts(self.find_current_print_ts(printer_state.status))
            else:
                printer_state.set_current_print_ts(-1)

        if cur_state == PrinterState.STATE_PRINTING:
            if prev_state == PrinterState.STATE_PAUSED:
                self.post_print_event('PrintResumed')
                return
            if prev_state == PrinterState.STATE_OPERATIONAL:
                printer_state.set_current_print_ts(self.find_current_print_ts(printer_state.status))
                self.post_print_event('PrintStarted')
                return

        if cur_state == PrinterState.STATE_PAUSED and prev_state == PrinterState.STATE_PRINTING:
            self.post_print_event('PrintPaused')
            return

        if cur_state == PrinterState.STATE_OPERATIONAL and prev_state in PrinterState.ACTIVE_STATES:
                _state = data['status'].get('print_stats', {}).get('state')
                if _state == 'cancelled':
                    self.post_print_event('PrintCancelled')
                    # PrintFailed as well to be consistent with OctoPrint
                    time.sleep(0.5)
                    self.post_print_event('PrintFailed')
                elif _state == 'complete':
                    self.post_print_event('PrintDone')
                elif _state == 'error':
                    self.post_print_event('PrintFailed')
                else:
                    # FIXME
                    _logger.error(
                        f'unexpected state "{_state}", please report.')

                printer_state.set_current_print_ts(-1)
                return

        self.server_conn.post_status_update_to_server()

    def process_server_msg(self, msg):
        _logger.debug(f'Received from server: {msg}')

        if 'remote_status' in msg:
            self.model.remote_status.update(msg['remote_status'])
            if self.model.remote_status['viewing']:
                self.jpeg_poster.need_viewing_boost.set()

        if 'commands' in msg:
            for command in msg['commands']:
                if command['cmd'] == 'pause':
                    # FIXME do we need this dance?
                    # self.commander.prepare_to_pause(
                    #    self._printer,
                    #    self._printer_profile_manager.get_current_or_default(),
                    #    **command.get('args'))
                    self.moonrakerconn.request_pause()

                if command['cmd'] == 'cancel':
                    self.moonrakerconn.request_cancel()

                if command['cmd'] == 'resume':
                    self.moonrakerconn.request_resume()

                # if command['cmd'] == 'print':
                #    self.start_print(**command.get('args'))

        if 'passthru' in msg:
            passthru = msg['passthru']
            target = (passthru.get('target'), passthru.get('func'))
            args = passthru.get('args', ())
            ack_ref = passthru.get('ref')

            if ack_ref is not None:
                # same msg may arrive through both ws and datachannel
                if ack_ref in self.model.seen_refs:
                    _logger.debug('Ignoring already processed passthru message')
                    return
                # no need to remove item or check size
                # as deque manages that when maxlen is set
                self.model.seen_refs.append(ack_ref)

            if target == ('file_downloader', 'download'):
                ret_value = self._process_download_message(ack_ref, gcode_file=args[0])

            elif target == ('_printer', 'jog'):
                ret_value = self._process_jog_message(ack_ref, axes_dict=args[0])

            elif target == ('_printer', 'home'):
                ret_value = self._process_home_message(ack_ref, axes=args[0])

            elif target == ('_printer', 'set_temperature'):
                ret_value = self._process_set_temperature_message(ack_ref, heater=args[0], target_temp=args[1])

            if ack_ref is not None:
                self.server_conn.send_ws_msg_to_server({'passthru': {'ref': ack_ref, 'ret': ret_value}})

        if msg.get('janus') and self.janus:
            self.janus.pass_to_janus(msg.get('janus'))

    def _process_download_message(self, ack_ref: str, gcode_file: Dict) -> None:
        if (
            not self.model.downloading_gcode_file and
            not self.model.printer_state.is_printing()
        ):

            thread = threading.Thread(
                target=self._download_and_print,
                args=(gcode_file, )
            )
            thread.daemon = True
            thread.start()

            self.model.downloading_gcode_file = True
            return {'target_path': gcode_file['filename']}
        else:
            return {'error': 'Currently downloading or printing!'}

    def _process_jog_message(self, ack_ref: str, axes_dict) -> None:
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

        _logger.info(f'jog request ({axes_dict}) with ack_ref {ack_ref}')
        self.moonrakerconn.request_jog(
            axes_dict=axes_dict, is_relative=is_relative, feedrate=feedrate
        )

    def _process_home_message(self, ack_ref: str, axes: List[str]) -> None:
        if not self.moonrakerconn:
            return {
                        'error': 'Printer is not connected!',
                    }

        _logger.info(f'homing request for {axes} with ack_ref {ack_ref}')
        self.moonrakerconn.request_home(axes=axes)

    def _process_set_temperature_message(self, ack_ref: str, heater, target_temp) -> None:
        if not self.moonrakerconn:
            return {
                        'error': 'Printer is not connected!',
                    }
        mr_heater = self.model.config.get_mapped_mr_heater_name(heater)
        if not mr_heater:
            _logger.error(f'Can not find corresponding heater for {heater} in Moonraker.')
        else:
            _logger.info(f'set_temperature request for {mr_heater} -> {target_temp} with ack_ref {ack_ref}')
        self.moonrakerconn.request_set_temperature(heater=mr_heater, target_temp=target_temp)


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
    args = parser.parse_args()
    App().start(args)
