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
from .ws import WebSocketClient, WebSocketConnectionException

_logger = logging.getLogger('obico.moonraker_conn')
_ignore_pattern=re.compile(r'"method": "notify_proc_stat_update"')

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
        self.q = queue.Queue(maxsize=1000)
        self.conn = None

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

        def on_mr_ws_open(ws):
            _logger.info('connection is ready')
            self.request_printer_info()
            self.request_subscribe()
            self.request_status_update()
            self.push_event(Event(sender=self.id, name='connected', data={}))


        def on_mr_ws_close(ws):
            self.push_event(
                Event(sender=self.id, name='mr_disconnected', data={'exc': None})
            )

        def on_message(ws, raw):
            if ( _ignore_pattern.search(raw) is not None ):
                return

            data = json.loads(raw)
            if data.get('method') == 'notify_klippy_disconnected':
                self.push_event(Event(sender=self.id, name='klippy_gone', data={}))
                return

            self.push_event(
                Event(sender=self.id, name='message', data=data)
            )


        reconn_backoff = ExpoBackoff(
            self.max_backoff_secs,
            max_attempts=None,
        )

        while self.shutdown is False:
            try:
                self.ensure_api_key()
                self.find_all_heaters()
                self.app_config.webcam.update_from_moonraker(self)

                if not self.conn or not self.conn.connected():
                    header=['X-Api-Key: {}'.format(self.config.api_key), ]
                    self.conn = WebSocketClient(
                                url=self.config.ws_url(),
                                header=header,
                                on_ws_msg=on_message,
                                on_ws_open=on_mr_ws_open,
                                on_ws_close=on_mr_ws_close,)
                    time.sleep(0.2)  # Wait for connection

                # _logger.debug('requesting last job')
                # self.request_job_list(order='desc', limit=1)
                # self.wait_for(self._received_last_job)

                reconn_backoff.reset()

                # forwarding events
                self.loop_forever(self.on_event)
            except WebSocketConnectionException as e:
                _logger.warning(e)
                reconn_backoff.more(e)
            except Exception as e:
                self.sentry.captureException(with_tags=True)
                reconn_backoff.more(e)

    def loop_forever(self, process_fn):
        while self.shutdown is False:
            event = self.q.get()
            self.on_event(event)

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
        self.shutdown = True
        if not self.conn:
            self.conn.close()

    def on_event(self, event):
        if self.shutdown:
            return

        self._on_event(event)

    # def _received_last_job(self, event):
    #     if 'jobs' in event.data.get('result', {}):
    #         jobs = event.data.get('result', {}).get('jobs', [None]) or [None]
    #         self.on_event(
    #             Event(sender=self.id, name='last_job', data=jobs[0])
    #         )
    #         return True

    def _jsonrpc_request(self, method, **params):
        if not self.conn or not self.conn.connected():
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


@dataclasses.dataclass
class Event:
    name: str
    data: Dict
    sender: Optional[str] = None