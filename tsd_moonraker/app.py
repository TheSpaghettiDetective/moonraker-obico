from __future__ import absolute_import
from typing import Optional, Dict, List
import argparse
import dataclasses
import time
import logging
import threading
import queue

import requests


from .wsconn import WSConn, ConnHandler
from .version import VERSION
from .utils import (
    get_tags, FlowTimeout,
    FatalError, Event, DEBUG, resp_to_exception, sanitize_filename)
from .webcam_capture import capture_jpeg
from .logger import getLogger, setup_logging
from .printer import PrinterState
from .config import MoonrakerConfig, TSDConfig, Config


logger = getLogger()

DEFAULT_LINKED_PRINTER = {'is_pro': False}
REQUEST_KLIPPY_STATE_TICKS = 10
POST_STATUS_INTERVAL_SECONDS = 50
POST_PIC_INTERVAL_SECONDS = 120

if DEBUG:
    POST_STATUS_INTERVAL_SECONDS = 10
    POST_PIC_INTERVAL_SECONDS = 10


class MoonrakerConn(ConnHandler):
    max_backoff_secs = 30
    flow_step_timeout_msecs = 2000
    ready_timeout_msecs = 60000

    class KlippyGone(Exception):
        pass

    def __init__(self, name, sentry, moonraker_config, on_event):
        super().__init__(name, sentry, on_event)
        self._next_id: int = 0
        self.config: MoonrakerConfig = moonraker_config
        self.websocket_id: Optional[int] = None
        self.printer_objects: Optional[list] = None
        self.heaters: Optional[List[str]] = None

    def next_id(self) -> int:
        next_id = self._next_id = self._next_id + 1
        return next_id

    def push_event(self, event):
        if self.shutdown:
            self.logger.debug(f'is shutdown, dropping event {event}')
            return False

        # removing some noise
        if event.data.get('method') == 'notify_proc_stat_update':
            return False

        return super().push_event(event)

    def prepare(self) -> None:
        # preparing and initalizing connection
        self.timer.reset(None)
        self.ready = False
        self.websocket_id = None

        if self.conn:
            self.conn.close()

        if not self.config.api_key:
            self.logger.warning('api key is unset, trying to fetch one')
            try:
                self.config.api_key = self.try_to_fetch_api_key()
            except Exception as exc:
                raise FatalError('no api key for moonraker', exc=exc)

        self.conn = WSConn(
            name=self.name,
            auth_header_fmt='X-Api-Key: {}',
            sentry=self.sentry,
            url=self.config.ws_url(),
            token=self.config.api_key,
            on_event=self.push_event,
            logger=getLogger(f'{self.name}.ws'),
        )

        self.conn.start()
        self.logger.debug('waiting for connection')
        self.wait_for(self._received_connected)

        self.logger.debug('requesting websocket_id')
        self.request_websocket_id()
        self.wait_for(self._received_websocket_id)

        while True:
            self.logger.info('waiting for klipper ready')
            self.ready = False
            try:
                while True:
                    rid = self.request_printer_info()
                    try:
                        self.wait_for(
                            self._received_printer_ready(rid),
                            self.ready_timeout_msecs)
                        break
                    except FlowTimeout:
                        continue

                self.logger.debug('requesting printer objects')
                self.request_printer_objects()
                self.wait_for(self._received_printer_objects)

                self.logger.debug('requesting heaters')
                self.request_heaters()
                self.wait_for(self._received_heaters)

                self.logger.debug('subscribing')
                sub_id = self.request_subscribe()
                self.wait_for(self._received_subscription(sub_id))

                self.logger.debug('requesting last job')
                self.request_job_list(order='desc', limit=1)
                self.wait_for(self._received_last_job)

                self.set_ready()
                self.logger.info('connection is ready')
                self.on_event(
                    Event(sender=self.name, name=f'{self.name}_ready', data={})
                )

                # forwarding events
                self.wait_for(self.on_event, None, loop_forever=True)
            except self.KlippyGone:
                self.logger.warning('klipper got disconnected')
                continue

    def _wait_for(self, event, process_fn, timeout_msecs):
        if (
            event.data.get('method') == 'notify_klippy_disconnected'
        ):
            self.on_event(Event(sender=self.name, name='klippy_gone', data={}))
            raise self.KlippyGone

        return super(MoonrakerConn, self)._wait_for(
            event, process_fn, timeout_msecs)

    def try_to_fetch_api_key(self):
        url = f'{self.config.canonical_endpoint_prefix()}/access/api_key'
        self.logger.debug(f'GET {url}')
        resp = requests.get(url, timeout=5)
        if resp.status_code in (401, 403):
            raise FatalError(
                f'{self.name} failed to fetch api key '
                f'(HTTP {resp.status_code})',
                exc=resp_to_exception(resp))

        resp.raise_for_status()
        return resp.json()['result']

    def _received_connected(self, event):
        if event.name == 'connected':
            return True

    def _received_printer_ready(self, id):
        def wait_for_id(event):
            if (
                (
                    'result' in event.data and
                    event.data['result'].get('state') == 'ready' and
                    event.data.get('id') == id
                ) or (
                    event.data.get('method') == 'notify_klippy_ready'
                )
            ):
                return True
        return wait_for_id

    def _received_websocket_id(self, event):
        if 'websocket_id' in event.data.get('result', ()):
            self.websocket_id = event.data['result']['websocket_id']
            return True

    def _received_printer_objects(self, event):
        if 'objects' in event.data.get('result', ()):
            self.printer_objects = event.data['result']['objects']
            self.logger.info(f'printer objects: {self.printer_objects}')
            return True

    def _received_heaters(self, event):
        if 'heaters' in event.data.get('result', {}).get('status', {}):
            self.heaters = event.data['result']['status']['heaters']['available_heaters']  # noqa: E501
            self.logger.info(f'heaters: {self.heaters}')
            return True

    def _received_subscription(self, sub_id):
        def wait_for_sub_id(event):
            if 'result' in event.data and event.data.get('id') == sub_id:
                return True
        return wait_for_sub_id

    def _received_last_job(self, event):
        if 'jobs' in event.data.get('result', {}):
            jobs = event.data.get('result', {}).get('jobs', [None])
            self.on_event(
                Event(sender=self.name, name='last_job', data=jobs[0])
            )
            return True

    def _jsonrpc_request(self, method, **params):
        if not self.conn:
            return

        next_id = self.next_id()
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": next_id
        }

        if params:
            payload['params'] = params

        self.conn.send(payload)
        return next_id

    def request_websocket_id(self):
        return self._jsonrpc_request('server.websocket.id')

    def request_printer_info(self):
        return self._jsonrpc_request('printer.info')

    def request_printer_objects(self):
        return self._jsonrpc_request('printer.objects.list')

    def request_heaters(self):
        objects = {'heaters': None}
        return self._jsonrpc_request('printer.objects.query', objects=objects)

    def request_subscribe(self, objects=None):
        objects = objects if objects else {
            'print_stats': ('state', 'message', 'filename'),
            'webhooks': ('state', 'state_message'),
            'history': None,
        }
        return self._jsonrpc_request('printer.objects.list', objects=objects)

    def request_status_update(self, objects=None):
        if objects is None:
            objects = {
                "webhooks": None,
                "print_stats": None,
                "virtual_sdcard": None,
                "display_status": None,
                "heaters": None,
                "toolhead": None,
                "extruder": None,
            }

            for heater in (self.heaters or ()):
                objects[heater] = None

        return self._jsonrpc_request('printer.objects.query', objects=objects)

    def request_pause(self):
        return self._jsonrpc_request('printer.print.pause')

    def request_cancel(self):
        return self._jsonrpc_request('printer.print.cancel')

    def request_resume(self):
        return self._jsonrpc_request('printer.print.resume')

    def request_job_list(self, **kwargs):
        # kwargs: start before since limit order
        return self._jsonrpc_request('server.history.list', **kwargs)

    def request_job(self, job_id):
        return self._jsonrpc_request('server.history.get_job', uid=job_id)

    def upload_gcode_over_http(self, filename, safe_filename, path, fileobj):
        url = f'{self.config.canonical_endpoint_prefix()}/server/files/upload'
        self.logger.debug('POST {url}')
        resp = requests.post(
            url,
            headers={'X-Api-Key': self.config.api_key},
            data={
                'path': path,
                'print': 'true'
            },
            files={
                'file': (filename, fileobj, 'application/octet-stream'),
            }
        )

        resp.raise_for_status()
        return resp.json()


class TSDConn(ConnHandler):
    max_backoff_secs = 300
    flow_step_timeout_msecs = 5000

    def __init__(self, name, sentry, tsd_config, on_event):
        super().__init__(name, sentry, on_event)
        self.config: TSDConfig = tsd_config

    def prepare(self):
        self.timer.reset(None)
        self.ready = False

        if self.conn:
            self.conn.close()

        self.conn = WSConn(
            name=self.name,
            auth_header_fmt='authorization: bearer {}',
            sentry=self.sentry,
            url=self.config.ws_url(),
            token=self.config.auth_token,
            on_event=self.push_event,
            logger=getLogger(f'{self.name}.ws'),
        )

        self.conn.start()

        self.logger.debug('waiting for connection')
        self.wait_for(self._received_connected)

        self.set_ready()
        self.logger.info('connection is ready')

    def _received_connected(self, event):
        if event.name == 'connected':
            return True

    def send_status_update(self, data):
        if self.ready:
            self.conn.send(data)

    def send_http_request(
        self, method, uri, timeout=10, raise_exception=True,
        **kwargs
    ):
        endpoint = self.config.canonical_endpoint_prefix() + uri
        headers = {
            'Authorization': f'Token {self.config.auth_token}'
        }
        headers.update(kwargs.pop('headers', {}))

        _kwargs = dict(allow_redirects=True)
        _kwargs.update(kwargs)

        self.logger.debug(f'{method} {endpoint}')
        try:
            resp = requests.request(
                method, endpoint, timeout=timeout, headers=headers, **_kwargs)
        except Exception:
            if raise_exception:
                raise
            return None

        if raise_exception:
            # if resp.status_code in (401, 403):
            #     raise AuthenticationError(
            #             f'HTTP {resp.status_code}',
            #             exc=resp_to_exception(resp))
            resp.raise_for_status()

        return resp

    def send_passthru(self, payload: Dict):
        if self.ready:
            self.conn.send({'passthru': payload})


class App(object):
    logger = logger

    @dataclasses.dataclass
    class Model:
        config: Config
        remote_status: Dict
        linked_printer: Dict
        printer_state: PrinterState
        last_print: Optional[Dict] = None
        status_update_booster: int = 0
        status_posted_to_server_ts: float = 0.0
        last_jpg_post_ts: float = 0.0
        downloading_gcode_file: Optional[Dict] = None
        posting_snapshot: bool = False

        def is_printing(self):
            return self.printer_state.is_printing()

        def is_configured(self):
            return True  # FIXME

    def __init__(self, model: Model):
        self.shutdown = False
        self.model = model
        self.sentry = self.model.config.get_sentry()
        self.tsdconn = None
        self.moonrakerconn = None
        self.q = queue.Queue(maxsize=1000)

    def push_event(self, event):
        if self.shutdown:
            self.logger.debug(f'is shutdown, dropping event {event}')
            return False

        try:
            self.q.put_nowait(event)
            return True
        except queue.Full:
            self.logger.error(f'event queue is full, dropping event {event}')
            return False

    def start(self):
        self.logger.info(f'starting tsd_moonraker (v{VERSION})')
        self.tsdconn = TSDConn(
            'tsdconn',
            self.sentry,
            self.model.config.thespaghettidetective,
            self.push_event,
        )

        self.moonrakerconn = MoonrakerConn(
            'moonrakerconn',
            self.sentry,
            self.model.config.moonraker,
            self.push_event,
        )

        thread = threading.Thread(
            target=self.tsdconn.start)
        thread.daemon = True
        thread.start()

        thread = threading.Thread(
            target=self.moonrakerconn.start)
        thread.daemon = True
        thread.start()

        thread = threading.Thread(
            target=self.scheduler_loop)
        thread.daemon = True
        thread.start()

        thread = threading.Thread(
            target=self.event_loop)
        thread.daemon = True
        thread.start()

        try:
            thread.join()
        except Exception:
            self.logger.exception('ops')

    def stop(self, cause=None):
        if cause:
            self.logger.error(f'shutdown ({cause})')
        else:
            self.logger.info('shutdown')

        self.shutdown = True
        if self.tsdconn:
            self.tsdconn.close()
        if self.moonrakerconn:
            self.moonrakerconn.close()

    def event_loop(self):
        # processes app events
        # alters state of app

        while self.shutdown is False:
            event = self.q.get()

            if event.name == 'fatal_error':
                self.stop(cause=event.data.get('exc'))

            elif event.name == 'shutdown':
                self.stop()

            elif event.sender == 'moonrakerconn':
                self._on_moonrakerconn_event(event)

            elif event.sender == 'tsdconn':
                self._on_tsdconn_event(event)

            elif event.name == 'download_and_print_done':
                self.logger.info('clearing downloading flag')
                self.model.downloading_gcode_file = None

            elif event.name == 'post_snapshot_done':
                self.logger.info('posting snapshot finished')
                self.model.posting_snapshot = False

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
                self.logger.debug(f'error response from moonraker, {event}')

            elif event.data.get('result') == "ok":
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

                # force sending status to tsd if current status is empty
                self._received_klippy_update(event.data['result'])

    def _on_tsdconn_event(self, event):
        if event.name == 'connected':
            # post latest klippy status when server gets connected
            # TODO add some delay?
            self.post_status_update()

        elif event.name == 'message':
            # message from tsd server
            self._received_server_message(event.data)

    def scheduler_loop(self, sleep_secs=1):
        # scheduler for events,
        # lightweight tasks only!
        loops = (
            self.recurring_klippy_status_request(),
            self.recurring_post_status_update(),
            self.recurring_post_snapshot(),
            # self.recurring_list_jobs_request(),
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

    def schedule_after_ticks(self, ticks, fn):
        return self._ticks_interval(ticks, fn, times=1, cur_counter=ticks)

    def recurring_klippy_status_request(self):
        def enqueue():
            if self.moonrakerconn.ready:
                self.moonrakerconn.request_status_update()

        return self._ticks_interval(REQUEST_KLIPPY_STATE_TICKS, enqueue)

    def recurring_list_jobs_request(self):
        def enqueue():
            if self.moonrakerconn.ready:
                self.moonrakerconn.request_job_list(limit=3, order='desc')

        return self._ticks_interval(5, enqueue)

    def recurring_post_status_update(self):
        while self.shutdown is False:
            interval_seconds = POST_STATUS_INTERVAL_SECONDS
            if self.model.status_update_booster > 0:
                self.model.status_update_booster -= 1
                interval_seconds /= 10

            t = time.time()
            if self.model.status_posted_to_server_ts < t - interval_seconds:
                self.model.status_posted_to_server_ts = time.time()
                self.post_status_update()

            yield

    def recurring_post_snapshot(self):
        while self.shutdown is False:
            interval_seconds = POST_PIC_INTERVAL_SECONDS

            if (
                self.model.is_configured() and
                self.model.is_printing()
            ):
                if (
                    not self.model.remote_status['viewing'] and
                    not self.model.remote_status['should_watch']
                ):
                    # slow down jpeg posting if needed
                    interval_seconds *= 12

                t = time.time()
                if self.model.last_jpg_post_ts < t - interval_seconds:
                    self.post_snapshot()

            yield

    def _capture_error(self, fn, args=(), kwargs=None, done_event_name=None):
        ret, data = None, {}
        try:
            ret = fn(*args, **(kwargs if kwargs is not None else {}))
        except Exception as exc:
            data['exc'] = exc
            self.logger.exception(
                f'unexpected error in {fn.__name__.lstrip("_")}')
            self.sentry.captureException()

        if done_event_name:
            self.push_event(Event(name=done_event_name, data=data))
        return ret

    def post_snapshot(self) -> None:
        if self.model.posting_snapshot:
            self.logger.info(
                'post_snapshot ignored; previous attempt has not finished')
            return

        thread = threading.Thread(
            target=self._capture_error(
                self._post_snapshot,
                done_event_name='post_snapshot_done'),
        )
        thread.daemon = True
        thread.start()

        self.model.last_jpg_post_ts = time.time()
        self.model.posting_snapshot = True

    def download_and_print(self, gcode_file: Dict) -> None:
        if self.model.downloading_gcode_file:
            self.logger.info(
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

        self.model.downloading_gcode_file = gcode_file

    def _post_snapshot(self) -> None:
        self.logger.info('capturing and posting snapshot')

        try:
            files = {
                'pic': capture_jpeg(self.model.config.webcam),
            }
        except (requests.exceptions.ConnectionError, ConnectionError) as exc:
            raise Exception(f'failed to capture snapshot ({exc})')

        self.tsdconn.send_http_request(
            'POST',
            '/api/v1/octo/pic/',
            timeout=60,
            files=files,
            raise_exception=True,
        )

    def _download_and_print(self, gcode_file):
        filename = gcode_file['filename']

        self.logger.info(
            f'downloading "{filename}" from {gcode_file["url"]}')

        safe_filename = sanitize_filename(filename)
        path = 'thespaghettidetective'

        r = requests.get(
            gcode_file['url'],
            allow_redirects=True,
            timeout=60 * 30
        )
        r.raise_for_status()

        self.logger.info(f'uploading "{filename}" to moonraker')
        resp_data = self.moonrakerconn.upload_gcode_over_http(
            filename, safe_filename, path, r.content
        )
        # if resp.status_code == 403:
        #     self.logger.info(f'got 403, upload might be loaded already')
        #     self.moonrakerconn.request_print(filename=f'{dirname}/{filename}')
        # else:

        self.logger.debug(f'upload response: {resp_data}')
        self.logger.info(
            f'uploading "{filename}" finished, print starting soon')

    def post_status_update(self, data=None):
        if not self.tsdconn.ready:
            return

        if not data:
            data = self.model.printer_state.to_tsd_state()

        # self.logger.debug(f'sending status to tsd: {data}')

        self.model.status_posted_to_server_ts = time.time()
        self.tsdconn.send_status_update(data)

    def post_print_event(self, print_event):
        ts = self.model.printer_state.current_print_ts
        if ts == -1:
            return

        self.logger.info(f'print event: {print_event} ({ts})')
        self.post_status_update(
            self.model.printer_state.to_tsd_state(print_event)
        )

    def _received_job_action(self, data):
        logger.info(data)
        self.model.last_print = data['job']

    def _received_last_print(self, job_data):
        self.logger.info(f'received last print: {job_data}')
        self.model.last_print = job_data
        self.model.printer_state.current_print_ts = int((
            self.model.last_print or {}
        ).get('start_time', -1))

    def _received_klippy_update(self, data):
        printer_state = self.model.printer_state

        prev_state_str = printer_state.get_state_str_from(printer_state.status)
        next_state_str = printer_state.get_state_str_from(data['status'])

        if prev_state_str != next_state_str:
            self.logger.info(
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
                last_print = self.model.last_print or {}
                last_print_ts = int(last_print.get('start_time', 0))

                if (
                    # if we have data about a very recently started print
                    last_print and
                    last_print.get('state') == 'in_progress' and
                    abs(ts - last_print_ts) < 20
                ):
                    # then let's use its timestamp
                    if ts != last_print_ts:
                        self.logger.debug(
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
                    self.logger.error(
                        f'unexpected state "{_state}", please report.')

                printer_state.current_print_ts = -1

    def _received_server_message(self, msg):
        logger.info(f'from tsd: {msg}')
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
            if target == ('file_downloader', 'download'):
                gcode_file = args[0]
                if (
                    not self.model.downloading_gcode_file and
                    not self.model.is_printing()
                ):
                    self.download_and_print(gcode_file)
                    self.tsdconn.send_passthru(
                        {
                            'ref': ack_ref,
                            'ret': {'target_path': gcode_file['filename']},
                        }
                    )
                else:
                    self.tsdconn.send_passthru(
                        {
                            'ref': ack_ref,
                            'ret': {
                                'error': 'Currently downloading or printing!'}
                        }
                    )

        # if msg.get('janus') and self.janus:
        #    self.janus.pass_to_janus(msg.get('janus'))

        if need_status_boost:
            self.boost_status_update()

    def boost_status_update(self):
        self.model.status_update_booster = 20


if __name__ == '__main__':
    parser = argparse.ArgumentParser('tsd_moonraker')
    parser.add_argument(
        '-c', '--config', dest='config_path', required=True,
        help='Path to config file (ini)'
    )
    parser.add_argument(
        '-l', '--log-file', dest='log_path', required=False,
        default='tsd_moonraker.log',
        help='Path to log file'
    )
    parser.add_argument(
        '-d', '--debug', dest='debug', required=False,
        action='store_true', default=False,
        help='Enable debug logging'
    )
    args = parser.parse_args()

    level = logging.DEBUG
    setup_logging(args.log_path, logging.DEBUG if args.debug else logging.INFO)

    config = Config.load_from(args.config_path)

    get_tags()

    model = App.Model(
        config=config,
        remote_status={'viewing': False, 'should_watch': False},
        linked_printer=DEFAULT_LINKED_PRINTER,
        printer_state=PrinterState(),
        last_print=None,
    )
    app = App(model)
    app.start()
