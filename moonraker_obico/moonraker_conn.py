from typing import Optional, Dict, List, Tuple
from numbers import Number
import dataclasses
import re
import queue
import threading
import requests  # type: ignore
import logging
import time
import backoff
import json
import bson
import websocket

from .utils import FlowTimeout, ShutdownException, FlowError, FatalError, ExpoBackoff


_logger = logging.getLogger('obico.moonraker_conn')

class MoonrakerConn:
    max_backoff_secs = 30
    flow_step_timeout_msecs = 2000
    ready_timeout_msecs = 60000

    class KlippyGone(Exception):
        pass

    def __init__(self, app_config, sentry, on_event):
        self.id: str = 'moonrakerconn'
        self._next_id: int = 0
        self.app_config: Config = app_config
        self.config: MoonrakerConfig = app_config.moonraker
        self.heaters: Optional[List[str]] = None

        self.sentry = sentry
        self._on_event = on_event
        self.shutdown: bool = False
        self.ready: bool = False
        self.q = queue.Queue(maxsize=1000)
        self.conn = None
        self.timer = Timer(self.push_event)
        self.reconn_backoff = ExpoBackoff(
            self.max_backoff_secs,
            max_attempts=None,
        )

    ## REST API part

    def api_get(self, mr_method, timeout=5, raise_for_status=True, **params):
        url = f'{self.config.http_address()}/{mr_method.replace(".", "/")}'
        _logger.debug(f'GET {url}')

        headers = {'X-Api-Key': self.config.api_key} if self.config.api_key else {}
        resp = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
        )

        if raise_for_status:
            resp.raise_for_status()

        return resp.json().get('result')

    def api_post(self, mr_method, filename=None, fileobj=None, **post_params):
        url = f'{self.config.http_address()}/{mr_method.replace(".", "/")}'
        _logger.debug(f'POST {url}')

        headers = {'X-Api-Key': self.config.api_key} if self.config.api_key else {}
        files={'file': (filename, fileobj, 'application/octet-stream')} if filename and fileobj else None
        resp = requests.post(
            url,
            headers=headers,
            data=post_params,
            files=files,
        )
        resp.raise_for_status()
        return resp.json()

    @backoff.on_exception(backoff.expo, Exception, max_value=60)
    def ensure_api_key(self):
        if not self.config.api_key:
            _logger.warning('api key is unset, trying to fetch one')
            self.config.api_key = self.api_get('access/api_key', raise_for_status=True)

    @backoff.on_exception(backoff.expo, Exception, max_value=60)
    def find_all_heaters(self):
        data = self.api_get('printer/objects/query', raise_for_status=True, heaters='') # heaters='' -> 'query?heaters=' by the behavior in requests
        if 'heaters' in data.get('status', {}):
            self.heaters = data['status']['heaters']['available_heaters']  # noqa: E501
            return True

    ## WebSocket part

    def start(self) -> None:
        self.timer.reset(None)
        self.ready = False

        self.ensure_api_key()
        self.find_all_heaters()

        self.conn = MoonrakerWSConn(
            id=self.id,
            auth_header_fmt='X-Api-Key: {}',
            sentry=self.sentry,
            url=self.config.ws_url(),
            token=self.config.api_key,
            on_event=self.push_event,
            ignore_pattern=re.compile(r'"method": "notify_proc_stat_update"')
        )

        self.conn.start()
        _logger.debug('waiting for connection')
        self.wait_for(self._received_connected)

        self.app_config.webcam.update_from_moonraker(self)

        while self.shutdown is False:
            _logger.info('waiting for klipper ready')
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

                _logger.debug('subscribing')
                sub_id = self.request_subscribe()
                self.wait_for(self._received_subscription(sub_id))

                _logger.debug('requesting last job')
                self.request_job_list(order='desc', limit=1)
                self.wait_for(self._received_last_job)

                self.ready = True
                self.reconn_backoff.reset()
                _logger.info('connection is ready')
                self.on_event(
                    Event(sender=self.id, name=f'{self.id}_ready', data={})
                )

                # forwarding events
                self.loop_forever(self.on_event)
            except self.KlippyGone:
                _logger.warning('klipper got disconnected')
                continue
            except FlowError as err:
                if hasattr(err, 'exc'):
                    _logger.error(f'{err} ({err.exc}), reconnecting')
                else:
                    _logger.error(f'got error ({err}), reconnecting')
                self.reconn_backoff.more(err)
            except FlowTimeout as err:
                _logger.error('got flow related timeout, reconnecting')
                self.reconn_backoff.more(err)
            except ShutdownException:
                _logger.error('shutting down')
                break
            except FatalError as exc:
                _logger.error(f'got fatal error ({exc})')
                self.on_event(
                    Event(
                        sender=self.id, name='fatal_error',
                        data={'exc': exc}
                    )
                )
                self.close()

    def loop_forever(self, process_fn):
        self.wait_for(process_fn, timeout_msecs=None, loop_forever=True)

    def next_id(self) -> int:
        next_id = self._next_id = self._next_id + 1
        return next_id

    def push_event(self, event):
        if self.shutdown:
            _logger.debug(f'is shutdown, dropping event {event}')
            return False

        try:
            self.q.put_nowait(event)
            return True
        except queue.Full:
            _logger.error(f'event queue is full, dropping {event}')
            return False

    def close(self):
        self.push_event(Event(name='shutdown', data={}))
        self.shutdown = True

    def on_event(self, event):
        if self.shutdown:
            return

        self._on_event(event)

    def wait_for(self, process_fn, timeout_msecs=-1, loop_forever=False):
        if timeout_msecs == -1:
            self.timer.reset(self.flow_step_timeout_msecs)
        else:
            self.timer.reset(timeout_msecs)

        while self.shutdown is False:
            event = self.q.get()

            if self._wait_for(event, process_fn, timeout_msecs):
                if not loop_forever:
                    return

    def _wait_for(self, event, process_fn, timeout_msecs):
        if event.data.get('method') == 'notify_klippy_disconnected':
            self.on_event(Event(sender=self.id, name='klippy_gone', data={}))
            raise self.KlippyGone

        if event.name == 'shutdown':
            self.shutdown = True
            if self.conn:
                self.conn.close()
            raise ShutdownException()

        if event.name == 'connection_error':
            self.ready = False
            self.on_event(event)
            exc = event.data.get('exc')
            if (
                exc and
                hasattr(exc, 'status_code') and
                exc.status_code in (401, 403)
            ):
                raise FatalError(f'{self.id} failed to authenticate', exc)

            message = str(exc) if exc else 'connection error'
            raise FlowError(message, exc=exc)

        if event.name == 'disconnected':
            self.ready = False
            self.on_event(event)
            raise FlowError('diconnected')

        if event.name == 'timeout' and event.data['timer_id'] == self.timer.id:
            raise FlowTimeout('timed out')

        if process_fn(event):
            return True

        return None

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

    def _received_subscription(self, sub_id):
        def wait_for_sub_id(event):
            if 'result' in event.data and event.data.get('id') == sub_id:
                return True
        return wait_for_sub_id

    def _received_last_job(self, event):
        if 'jobs' in event.data.get('result', {}):
            jobs = event.data.get('result', {}).get('jobs', [None]) or [None]
            self.on_event(
                Event(sender=self.id, name='last_job', data=jobs[0])
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
                "gcode_move": None,
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

    def request_jog(self, axes_dict: Dict[str, Number], is_relative: bool, feedrate: int) -> Dict:
        # TODO check axes
        command = "G0 {}".format(
            " ".join([
                "{}{}".format(axis.upper(), amt)
                for axis, amt in axes_dict.items()
            ])
        )

        if feedrate:
            command += " F{}".format(feedrate * 60)

        commands = ["G91", command]
        if not is_relative:
            commands.append("G90")

        script = "\n".join(commands)
        return self._jsonrpc_request('printer.gcode.script', script=script)

    def request_home(self, axes) -> Dict:
        # TODO check axes
        script = "G28 %s" % " ".join(
            map(lambda x: "%s0" % x.upper(), axes)
        )
        return self._jsonrpc_request('printer.gcode.script', script=script)

class Timer(object):

    def __init__(self, push_event):
        self.id = 0
        self.push_event = push_event

    def reset(self, timeout_msecs):
        self.id += 1
        if timeout_msecs is not None:
            thread = threading.Thread(
                target=self.ticktack,
                args=(self.id, timeout_msecs)
            )
            thread.daemon = True
            thread.start()

    def ticktack(self, timer_id, msecs):
        time.sleep(msecs / 1000.0)

        if self.id != timer_id:
            return

        self.push_event(Event(name='timeout', data={'timer_id': timer_id}))


@dataclasses.dataclass
class Event:
    name: str
    data: Dict
    sender: Optional[str] = None


class MoonrakerWSConn(object):

    def __init__(
        self, id, sentry, url, token, on_event, auth_header_fmt,
        subprotocols=None, ignore_pattern=None
    ):
        self.shutdown = False
        self.id = id
        self.sentry = sentry
        self.url = url
        self.token = token
        self._on_event = on_event
        self.to_server_q = queue.Queue(maxsize=1000)
        self.wsock = None
        self.auth_header_fmt = auth_header_fmt
        self.subprotocols = subprotocols
        self.ignore_pattern = ignore_pattern

    def send(self, data, is_binary=False):
        try:
            self.to_server_q.put_nowait((data, is_binary, False))
        except queue.Full:
            _logger.exception('sending queue is full')

    def on_event(self, event):
        if self.shutdown:
            return

        self._on_event(event)

    def close(self):
        self.shutdown = True
        try:
            self.to_server_q.put_nowait((None, False, True))
        except queue.Full:
            _logger.exception('sending queue is full')

    def start(self):
        server_thread = threading.Thread(
            target=self.sender_loop)
        server_thread.daemon = True
        server_thread.start()

    def _connect_websocket(self):
        def on_ws_error(ws, error):
            _logger.debug(f'connection error ({error})')
            if self.wsock:
                if self.wsock != ws:
                    return

                self.wsock.close()
                self.wsock = None

                try:
                    self.on_event(
                        Event(
                            sender=self.id,
                            name='connection_error',
                            data={'exc': error},
                        )
                    )
                except queue.Full:
                    self.sentry.captureException(with_tags=True)

        def on_ws_close(ws, *args, **kwargs):
            _logger.debug('connection closed')
            if self.wsock and self.wsock == ws:
                self.wsock = None
                try:
                    self.on_event(
                        Event(
                            sender=self.id,
                            name='disconnected',
                            data={'exc': None}
                        )
                    )
                except queue.Full:
                    self.sentry.captureException(with_tags=True)

        def on_ws_open(ws):
            if self.wsock:
                if self.wsock != ws:
                    return

                try:
                    self.on_event(
                        Event(sender=self.id, name='connected', data={}))
                except queue.Full:
                    self.sentry.captureException(with_tags=True)

        def on_ws_message(ws, raw):
            if (
                self.ignore_pattern and
                self.ignore_pattern.search(raw) is not None
            ):
                return

            try:
                self.on_event(
                    Event(
                        sender=self.id, name='message', data=json.loads(raw)
                    )
                )
            except queue.Full:
                self.sentry.captureException(with_tags=True)

        _logger.info(f'connecting to {self.url}')
        self.wsock = websocket.WebSocketApp(
            self.url,
            on_message=on_ws_message,
            on_open=on_ws_open,
            on_close=on_ws_close,
            on_error=on_ws_error,
            header=[self.auth_header_fmt.format(self.token), ],
            subprotocols=self.subprotocols,
        )

        wst = threading.Thread(
            target=self.wsock.run_forever,
            kwargs=dict(
                ping_interval=20,
                ping_timeout=10,
            )
        )
        wst.daemon = True
        wst.start()

    def sender_loop(self):
        try:
            self._connect_websocket()
            while self.shutdown is False:
                (data, as_binary, shutdown) = self.to_server_q.get()

                if shutdown:
                    self.shutdown = True
                    if self.wsock:
                        self.wsock.close()
                    break

                if as_binary:
                    raw = bson.dumps(data)
                    opcode = websocket.ABNF.OPCODE_BINARY
                else:
                    raw = json.dumps(data, default=str)
                    opcode = websocket.ABNF.OPCODE_TEXT

                if (
                    self.wsock and
                    self.wsock.sock and
                    self.wsock.sock.connected
                ):
                    _logger.debug(f'sending {raw}')
                    self.wsock.send(raw, opcode=opcode)
                else:
                    _logger.error(f'unable to send {raw}')
        except Exception as e:
            try:
                self.on_event(
                    Event(
                        sender=self.id,
                        name='connection_error',
                        data={'exception': e})
                )
            except queue.Full:
                self.sentry.captureException(with_tags=True)
            _logger.warning(e)