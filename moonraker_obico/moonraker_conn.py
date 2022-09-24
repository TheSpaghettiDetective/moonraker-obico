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
from collections import deque

from .utils import DEBUG
from .ws import WebSocketClient, WebSocketConnectionException


REQUEST_STATE_INTERVAL_SECONDS = 30
if DEBUG:
    REQUEST_STATE_INTERVAL_SECONDS = 10

_logger = logging.getLogger('obico.moonraker_conn')
_ignore_pattern=re.compile(r'"method": "notify_proc_stat_update"')

class MoonrakerConn:
    flow_step_timeout_msecs = 2000
    ready_timeout_msecs = 60000

    def __init__(self, app_config, sentry, on_event):
        self.id: str = 'moonrakerconn'
        self._next_id: int = 0
        self.app_config: Config = app_config
        self.config: MoonrakerConfig = app_config.moonraker
        self.klippy_ready = threading.Event()  # Based on https://moonraker.readthedocs.io/en/latest/web_api/#websocket-setup

        self.sentry = sentry
        self._on_event = on_event
        self.shutdown: bool = False
        self.conn = None
        self.ws_message_queue_to_moonraker = queue.Queue(maxsize=16)
        self.moonraker_state_requested_ts = 0
        self.status_update_request_ids = deque(maxlen=25)  # contains "last" 25 status_update_request_ids

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
    def get_server_info(self):
        return self.api_get('server/info')

    @backoff.on_exception(backoff.expo, Exception, max_value=60)
    @backoff.on_predicate(backoff.expo, max_value=60)
    def wait_for_klippy_ready(self):
        return self.get_server_info().get("klippy_state") == 'ready'

    @backoff.on_exception(backoff.expo, Exception, max_value=60)
    def find_all_heaters(self):
        data = self.api_get('printer/objects/query', raise_for_status=True, heaters='') # heaters='' -> 'query?heaters=' by the behavior in requests
        if 'heaters' in data.get('status', {}):
            return data['status']['heaters']
        else:
            return []

    @backoff.on_exception(backoff.expo, Exception, max_value=60)
    def find_most_recent_job(self):
        data = self.api_get('server/history/list', raise_for_status=True, order='desc', limit=1)
        return (data.get('jobs', [None]) or [None])[0]

    def update_webcam_config_from_moonraker(self):
        def webcam_config_in_moonraker():
            # Check for the standard namespace for webcams
            result = self.api_get('server.database.item', raise_for_status=False, namespace='webcams')
            if result:
                _logger.debug(f'Found config in Moonraker webcams namespace: {result}')
                # TODO: Just pick the last webcam before we have a way to support multiple cameras
                for cfg in result.get('value', {}).values():
                    return dict(
                        snapshot_url = cfg.get('urlSnapshot', None),
                        stream_url = cfg.get('urlStream', None),
                        flip_h = cfg.get('flipX', False),
                        flip_v = cfg.get('flipY', False),
                    )

            # webcam configs not found in the standard location. Try fluidd's flavor
            result = self.api_get('server.database.item', raise_for_status=False, namespace='fluidd', key='cameras')
            if result:
                _logger.debug(f'Found config in Moonraker fluidd/cameras namespace: {result}')
                # TODO: Just pick the last webcam before we have a way to support multiple cameras
                for cfg in result.get('value', {}).get('cameras', []):
                    if not cfg.get('enabled', False):
                        continue

                    return dict(
                        stream_url = cfg.get('url', None),
                        flip_h = cfg.get('flipX', False),
                        flip_v = cfg.get('flipY', False),
                    )

            #TODO: Send notification to user that webcam configs not found when moonraker's announcement api makes to stable

        mr_webcam_config = webcam_config_in_moonraker()
        if mr_webcam_config:
            _logger.debug(f'Retrieved webcam config from Moonraker: {mr_webcam_config}')
            self.app_config.webcam.moonraker_webcam_config = mr_webcam_config
        else:
            #TODO: Send notification to user that webcam configs not found when moonraker's announcement api makes to stable
            pass


    ## WebSocket part

    def start(self) -> None:

        thread = threading.Thread(target=self.message_to_moonraker_loop)
        thread.daemon = True
        thread.start()

        while self.shutdown is False:
            try:
                if self.klippy_ready.wait() and self.moonraker_state_requested_ts < time.time() - REQUEST_STATE_INTERVAL_SECONDS:
                    self.request_status_update()

            except Exception as e:
                self.sentry.captureException()

            time.sleep(1)

    def message_to_moonraker_loop(self):

        def on_mr_ws_open(ws):
            _logger.info('connection is open')

            self.wait_for_klippy_ready()

            self.app_config.update_heater_mapping(self.find_all_heaters())  # We need to find all heaters as their names have to be specified in the objects query request
            self.klippy_ready.set()

            self.request_subscribe()

        def on_mr_ws_close(ws, **kwargs):
            self.klippy_ready.clear()
            self.push_event(
                Event(sender=self.id, name='mr_disconnected', data={})
            )
            self.request_status_update()  # Trigger a re-connection to Moonraker

        def on_message(ws, raw):
            if ( _ignore_pattern.search(raw) is not None ):
                return

            data = json.loads(raw)
            _logger.debug(f'Received from Moonraker: {data}')

            if data.get('id', -1) in self.status_update_request_ids and 'result' in data:
                self.push_event(
                    Event(sender=self.id, name='status_update', data=data)
                )
                return

            self.push_event(
                Event(sender=self.id, name='message', data=data)
            )

        self.request_status_update()  # "Seed" a request in ws_message_queue_to_moonraker to trigger the initial connection to Moonraker

        while self.shutdown is False:
            try:
                data = self.ws_message_queue_to_moonraker.get()

                if not self.conn or not self.conn.connected():
                    self.ensure_api_key()

                    if not self.conn or not self.conn.connected():
                        header=['X-Api-Key: {}'.format(self.config.api_key), ]
                        self.conn = WebSocketClient(
                                    url=self.config.ws_url(),
                                    header=header,
                                    on_ws_msg=on_message,
                                    on_ws_open=on_mr_ws_open,
                                    on_ws_close=on_mr_ws_close,)

                        self.klippy_ready.wait()
                        _logger.info('Klippy ready')

                _logger.debug("Sending to Moonraker: \n{}".format(data))
                self.conn.send(json.dumps(data, default=str))
            except WebSocketConnectionException as e:
                _logger.warning(e)
            except Exception as e:
                _logger.warning(e)
                self.sentry.captureException()

    def next_id(self) -> int:
        next_id = self._next_id = self._next_id + 1
        return next_id

    def push_event(self, event):
        if self.shutdown:
            return

        self._on_event(event)

    def close(self):
        self.shutdown = True
        if not self.conn:
            self.conn.close()

    def _jsonrpc_request(self, method, **params):
        next_id = self.next_id()
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": next_id
        }

        if params:
            payload['params'] = params

        try:
            self.ws_message_queue_to_moonraker.put_nowait(payload)
        except queue.Full:
            _logger.warning("Moonraker message queue is full, msg dropped")

        return next_id

    def request_subscribe(self, objects=None):
        objects = objects if objects else {
            'print_stats': ('state', 'message', 'filename'),
            'webhooks': ('state', 'state_message'),
            'history': None,
        }
        return self._jsonrpc_request('printer.objects.subscribe', objects=objects)

    def request_status_update(self, objects=None):
        self.moonraker_state_requested_ts = time.time()

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

            for heater in (self.app_config.all_mr_heaters()):
                objects[heater] = None

        self.status_update_request_ids.append(self._jsonrpc_request('printer.objects.query', objects=objects))

    def request_pause(self):
        return self._jsonrpc_request('printer.print.pause')

    def request_cancel(self):
        return self._jsonrpc_request('printer.print.cancel')

    def request_resume(self):
        return self._jsonrpc_request('printer.print.resume')

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

    def request_set_temperature(self, heater, target_temp) -> Dict:
        script = f'SET_HEATER_TEMPERATURE HEATER={heater} TARGET={target_temp}'
        return self._jsonrpc_request('printer.gcode.script', script=script)


@dataclasses.dataclass
class Event:
    name: str
    data: Dict
    sender: Optional[str] = None
