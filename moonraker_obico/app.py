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

import requests  # type: ignore

from .wsconn import WSConn, Event
from .version import VERSION
from .utils import get_tags, FatalError, DEBUG, resp_to_exception, sanitize_filename
from .webcam_capture import capture_jpeg
from .logger import setup_logging
from .printer import PrinterState
from .config import MoonrakerConfig, ServerConfig, Config
from .moonraker_conn import MoonrakerConn
from .server_conn import ServerConn
from .webcam_stream import WebcamStreamer
from .janus import JanusConn


_logger = logging.getLogger('obico.app')
_default_int_handler = None
_default_term_handler = None

DEFAULT_LINKED_PRINTER = {'is_pro': False}
REQUEST_KLIPPY_STATE_TICKS = 10
POST_STATUS_INTERVAL_SECONDS = 50
POST_PIC_INTERVAL_SECONDS = 10

if DEBUG:
    POST_STATUS_INTERVAL_SECONDS = 10
    POST_PIC_INTERVAL_SECONDS = 10


ACKREF_EXPIRE_SECS = 300


class App(object):

    @dataclasses.dataclass
    class Model:
        config: Config
        remote_status: Dict
        linked_printer: Dict
        printer_state: PrinterState
        force_snapshot: threading.Event
        seen_refs: collections.deque
        last_jpg_post_ts: float = 0.0
        downloading_gcode_file: Optional[Tuple[str, Dict]] = None

        def is_printing(self):
            return self.printer_state.is_printing()

        def is_configured(self):
            return True  # FIXME

    def __init__(self, model: Model):
        self.shutdown = False
        self.model = model
        self.sentry = self.model.config.get_sentry()
        self.server_conn = None
        self.moonrakerconn = None
        self.webcam_streamer = None
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

    def start(self):
        # TODO: This doesn't work as ffmpeg seems to mess with signals as well
        # global _default_int_handler, _default_term_handler
        # _default_int_handler = signal.signal(signal.SIGINT, self.interrupted)
        # _default_term_handler = signal.signal(signal.SIGTERM, self.interrupted)

        _logger.info(f'starting moonraker-obico (v{VERSION})')
        _logger.debug(self.model.config.server)
        self.server_conn = ServerConn(self.model.config.server, self.model.printer_state, self.process_server_msg, self.sentry, )
        self.moonrakerconn = MoonrakerConn(self.model.config, self.sentry, self.push_event,)
        self.janus = JanusConn(self.model.config, self.server_conn, self.sentry)

        # Blocking call. When continued, server is guaranteed to be properly configured, self.model.linked_printer existed.
        self.model.linked_printer = self.server_conn.get_linked_printer()

        if self.model.linked_printer.get('is_pro') and not self.model.config.webcam.disable_video_streaming:
            _logger.info('Starting webcam streamer')
            self.webcam_streamer = WebcamStreamer(self.model.config, self.sentry)
            stream_thread = threading.Thread(target=self.webcam_streamer.video_pipeline)
            stream_thread.daemon = True
            stream_thread.start()

        thread = threading.Thread(target=self.server_conn.start)
        thread.daemon = True
        thread.start()

        thread = threading.Thread(target=self.moonrakerconn.start)
        thread.daemon = True
        thread.start()

        thread = threading.Thread(target=self.snapshot_loop)
        thread.daemon = True
        thread.start()

        thread = threading.Thread(target=self.scheduler_loop)
        thread.daemon = True
        thread.start()

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
            _logger.exception('ops')

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
                self.sentry.captureException()
                _logger.exception(f'error processing event {event}')

    def _process_event(self, event):
        if event.name == 'fatal_error':
            self.stop(cause=event.data.get('exc'))

        elif event.name == 'shutdown':
            self.stop()

        elif event.sender == 'moonrakerconn':
            self._on_moonrakerconn_event(event)

        elif event.name == 'download_and_print_done':
            _logger.info('clearing downloading flag')
            self.model.downloading_gcode_file = None

        elif event.name == 'post_snapshot_done':
            _logger.info('posting snapshot finished')

    def _on_moonrakerconn_event(self, event):
        if event.name in ('disconnected', 'connection_error', 'klippy_gone'):
            # clear app's klippy state
            self._received_klippy_update(
                {
                    "status": {},
                    "eventtime": 0.0
                }
            )

        elif event.name == 'moonrakerconn_ready':
            # moonraker connection is up and initalized,
            # let's request a full state update
            self.moonrakerconn.request_status_update()

        elif event.name == 'last_job':
            self._received_last_print(event.data)

        elif event.name == 'message':
            if 'error' in event.data:
                _logger.debug(f'error response from moonraker, {event}')

            elif event.data.get('result') == 'ok':
                # printer action response
                self.moonrakerconn.request_status_update()

            elif event.data.get('method', '') == 'notify_status_update':
                # something important has changed,
                # fetching full status
                self.moonrakerconn.request_status_update()

            elif event.data.get('method', '') == 'notify_history_changed':
                for item in event.data['params']:
                    self._received_job_action(item)
                self.moonrakerconn.request_status_update()

            elif 'status' in event.data.get('result', ()):
                # full state update from moonraker
                self._received_klippy_update(event.data['result'])


    def scheduler_loop(self, sleep_secs=1):
        # scheduler for events,
        # lightweight tasks only!
        loops = (
            self._recurring_klippy_status_request(),
            self._recurring_post_snapshot(),
            # self._recurring_list_jobs_request(),
        )
        while self.shutdown is False:
            for loop in loops:
                next(loop)
            time.sleep(sleep_secs)

    def _ticks_interval(self, interval_ticks, fn, times=None, cur_counter=0):
        tick_counter = cur_counter
        while self.shutdown is False:
            tick_counter -= 1
            if tick_counter < 1:
                tick_counter = interval_ticks

                fn()

                if times is not None:
                    times -= 1
                    if times <= 0:
                        return

            yield

    def _schedule_after_ticks(self, ticks, fn):
        return self._ticks_interval(ticks, fn, times=1, cur_counter=ticks)

    def _recurring_klippy_status_request(self):
        def enqueue():
            if self.moonrakerconn.ready:
                self.moonrakerconn.request_status_update()

        return self._ticks_interval(REQUEST_KLIPPY_STATE_TICKS, enqueue)

    def _recurring_list_jobs_request(self):
        def enqueue():
            if self.moonrakerconn.ready:
                self.moonrakerconn.request_job_list(limit=3, order='desc')

        return self._ticks_interval(5, enqueue)

    def _recurring_post_snapshot(self):
        while self.shutdown is False:
            now = time.time()
            interval_seconds = POST_PIC_INTERVAL_SECONDS

            if (
                not self.model.is_printing() and
                not self.model.remote_status['viewing'] and
                not self.model.remote_status['should_watch']
            ):
                # slow down jpeg posting if needed
                interval_seconds *= 12

            if self.model.last_jpg_post_ts < now - interval_seconds:
                self.post_snapshot()
                self.model.last_jpg_post_ts = now

            yield

    def _capture_error(self, fn, args=(), kwargs=None, done_event_name=None):
        ret, data = None, {}
        try:
            ret = fn(*args, **(kwargs if kwargs is not None else {}))
            data['ret'] = ret
        except Exception as exc:
            data['exc'] = exc
            _logger.exception(
                f'unexpected error in {fn.__name__.lstrip("_")}')
            self.sentry.captureException()

        if done_event_name:
            self.push_event(Event(name=done_event_name, data=data))
        return ret

    def post_snapshot(self) -> None:
        if self.server_conn:
            self.model.force_snapshot.set()

    def snapshot_loop(self):
        while self.shutdown is False:
            if self.model.force_snapshot.wait(2) is True:
                self.model.force_snapshot.clear()
                self.model.last_jpg_post_ts = time.time()
                self._capture_error(
                    self._post_snapshot,
                    done_event_name='post_snapshot_done',
                )

    def download_and_print(self, ref: str, gcode_file: Dict) -> None:
        if self.model.downloading_gcode_file:
            _logger.info(
                'download_and_print ignored; previous attempt has not finished'
            )
            return

        thread = threading.Thread(
            target=self._capture_error(
                self._download_and_print,
                args=(gcode_file, ),
                done_event_name='download_and_print_done',
            )
        )
        thread.daemon = True
        thread.start()

        self.model.downloading_gcode_file = (ref, gcode_file)

    def _post_snapshot(self) -> None:
        if not self.server_conn:
            return

        _logger.info('capturing and posting snapshot')

        pic = capture_jpeg(self.model.config.webcam)
        if not pic:
            _logger.error('Error in capture_jpeg. Skipping posting snapshot...') # Likely due to mistaken configuration. Not reporting to sentry.
            return

        self.server_conn.send_http_request(
            'POST',
            '/api/v1/octo/pic/',
            timeout=60,
            files={'pic': pic},
            raise_exception=True,
        )

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

    def post_status_update_to_server(self, data=None, config=None):
        if not data:
            data = self.model.printer_state.to_dict(config=config)

        self.model.status_posted_to_server_ts = time.time()
        self.server_conn.send_ws_msg_to_server(data)

    def post_print_event(self, print_event, config=None):
        ts = self.model.printer_state.current_print_ts
        if ts == -1:
            return

        _logger.info(f'print event: {print_event} ({ts})')
        self.post_status_update_to_server(
            self.model.printer_state.to_dict(print_event, config=config)
        )

    def _received_job_action(self, data):
        _logger.info(f'received print: {data["job"]}')
        self.model.printer_state.last_print = data['job']

    def _received_last_print(self, job_data):
        _logger.info(f'received last print: {job_data}')
        self.model.printer_state.last_print = job_data
        self.model.printer_state.current_print_ts = int((
            self.model.printer_state.last_print or {}
        ).get('start_time', -1))

    def _received_klippy_update(self, data):
        printer_state = self.model.printer_state

        prev_state_str = printer_state.get_state_str_from(printer_state.status)
        next_state_str = printer_state.get_state_str_from(data['status'])

        if prev_state_str != next_state_str:
            _logger.info(
                'detected state change: {} -> {}'.format(
                    prev_state_str, next_state_str
                )
            )
            self.boost_status_update()

        printer_state.eventtime = data['eventtime']
        printer_state.status = data['status']

        if next_state_str == 'Printing':
            if prev_state_str == 'Printing':
                pass
            elif prev_state_str == 'Paused':
                self.post_print_event('PrintResumed')
            else:
                ts = int(time.time())
                last_print = printer_state.last_print or {}
                last_print_ts = int(last_print.get('start_time', 0))

                if (
                    # if we have data about a very recently started print
                    last_print and
                    last_print.get('state') == 'in_progress' and
                    abs(ts - last_print_ts) < 20
                ):
                    # then let's use its timestamp
                    if ts != last_print_ts:
                        _logger.debug(
                            "choosing moonraker's job start_time "
                            "as current_print_ts")
                    ts = last_print_ts

                printer_state.current_print_ts = ts
                self.post_print_event('PrintStarted')

        elif next_state_str == 'Offline':
            pass

        elif next_state_str == 'Paused':
            if prev_state_str != 'Paused':
                self.post_print_event('PrintPaused')

        elif next_state_str == 'Error':
            if prev_state_str != 'Error':
                self.post_print_event('PrintFailed')
                printer_state.current_print_ts = -1

        elif next_state_str == 'Operational':
            if prev_state_str in ('Paused', 'Printing'):
                _state = data['status'].get('print_stats', {}).get('state')
                if _state == 'cancelled':
                    self.post_print_event('PrintCancelled')
                    # somehow failed is expected too
                    self.post_print_event('PrintFailed')
                elif _state == 'complete':
                    self.post_print_event('PrintDone')
                else:
                    # FIXME
                    _logger.error(
                        f'unexpected state "{_state}", please report.')

                printer_state.current_print_ts = -1

    def process_server_msg(self, msg):
        _logger.debug(f'Received from server: {msg}')
        need_status_boost = False

        if 'remote_status' in msg:
            self.model.remote_status.update(msg['remote_status'])
            if self.model.remote_status['viewing']:
                self.post_snapshot()
            need_status_boost = True

        if 'commands' in msg:
            need_status_boost = True
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
            need_status_boost = True
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

            if ack_ref is not None:
                self.server_conn.send_ws_msg_to_server({'passthru': {'ref': ack_ref, 'ret': ret_value}})

        if msg.get('janus') and self.janus:
            self.janus.pass_to_janus(msg.get('janus'))

        if need_status_boost:
            self.boost_status_update()

    def boost_status_update(self):
        self.server_conn.status_update_booster = 20

    def _process_download_message(self, ack_ref: str, gcode_file: Dict) -> None:
        if (
            not self.model.downloading_gcode_file and
            not self.model.is_printing()
        ):
            self.download_and_print(ack_ref, gcode_file)
            return {'target_path': gcode_file['filename']}
        else:
            return {'error': 'Currently downloading or printing!'}

    def _process_jog_message(self, ack_ref: str, axes_dict) -> None:
        if not self.moonrakerconn or not self.moonrakerconn.ready:
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
        if not self.moonrakerconn or not self.moonrakerconn.ready:
            return {
                        'error': 'Printer is not connected!',
                    }

        _logger.info(f'homing request for {axes} with ack_ref {ack_ref}')
        self.moonrakerconn.request_home(axes=axes)


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

    config = Config.load_from(args.config_path)

    if args.log_path:
        config.logging.path = args.log_path
    if args.debug:
        config.logging.level = 'DEBUG'
    setup_logging(config.logging)

    get_tags()

    model = App.Model(
        config=config,
        remote_status={'viewing': False, 'should_watch': False},
        linked_printer=DEFAULT_LINKED_PRINTER,
        printer_state=PrinterState(),
        force_snapshot=threading.Event(),
        seen_refs=collections.deque(maxlen=100),
    )
    app = App(model)
    app.start()
