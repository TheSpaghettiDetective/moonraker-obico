from typing import Optional, Dict, List, Tuple
import requests  # type: ignore
import logging
import time
import queue
import bson
import json
from collections import deque
import backoff
from urllib.error import URLError, HTTPError

from .utils import ExpoBackoff, DEBUG, run_in_thread
from .ws import WebSocketClient, WebSocketConnectionException
from .config import Config
from .printer import PrinterState
from .webcam_capture import capture_jpeg
from .lib import curlify

NON_CRITICAL_UPDATE_INTERVAL_SECONDS = 30
if DEBUG:
    NON_CRITICAL_UPDATE_INTERVAL_SECONDS = 5

_logger = logging.getLogger('obico.server_conn')

class ServerConn:

    def __init__(self, config: Config, printer_state: PrinterState, process_server_msg, sentry):
        self.should_reconnect = True
        self.config: Config = config
        self.printer_state: PrinterState() = printer_state
        self.process_server_msg = process_server_msg
        self.sentry = sentry

        self.status_posted_to_server_ts = 0
        self.ss = None
        self.message_queue_to_server = queue.Queue(maxsize=50)
        self.printer_events_posted = deque(maxlen=20)


    ## WebSocket part of the server connection

    def start(self):

        def on_server_ws_close(ws, close_status_code):
            if self.ss and self.ss.ws and self.ss.ws == ws:
                self.ss = None

            if close_status_code == 4321:
                _logger.warning('Shared auth_token detected. Shutting down.')
                self.should_reconnect = False

        def on_server_ws_open(ws):
            self.post_status_update_to_server(with_settings=True) # Make sure an update is sent asap so that the server can rely on the availability of essential info such as agent.version

        def on_message(ws, msg):
            try:
                decoded = json.loads(msg)
            except ValueError:
                decoded = bson.loads(msg)

            self.process_server_msg(decoded)

        server_ws_backoff = ExpoBackoff(300)
        self.send_ws_msg_to_server({}) # Initial null message to trigger server connection

        while self.should_reconnect:
            try:
                (data, as_binary) = self.message_queue_to_server.get()

                if not self.ss or not self.ss.connected():
                    header = ["authorization: bearer " + self.config.server.auth_token]
                    self.ss = WebSocketClient(
                        self.config.server.ws_url(),
                        header=header,
                        on_ws_msg=on_message,
                        on_ws_open=on_server_ws_open,
                        on_ws_close=on_server_ws_close,)

                if as_binary:
                    raw = bson.dumps(data)
                else:
                    _logger.debug("Sending to server: \n{}".format(data))
                    raw = json.dumps(data, default=str)
                self.ss.send(raw, as_binary=as_binary)
                server_ws_backoff.reset()
            except WebSocketConnectionException as e:
                _logger.warning(e)
                server_ws_backoff.more(e)
            except Exception as e:
                self.sentry.captureException()
                server_ws_backoff.more(e)


    def send_ws_msg_to_server(self, data, as_binary=False):
        try:
            self.message_queue_to_server.put_nowait((data, as_binary))
        except queue.Full:
            _logger.warning("Server message queue is full, msg dropped")

    def post_status_update_to_server(self, print_event: Optional[str] = None, with_settings: Optional[bool] = False, is_critical=True):
        # Throttle the non-critical updates to the server to reduce the server load
        if not (print_event or with_settings or is_critical) and self.status_posted_to_server_ts > time.time() - NON_CRITICAL_UPDATE_INTERVAL_SECONDS:
            return

        self.send_ws_msg_to_server(self.printer_state.to_dict(print_event=print_event, with_settings=with_settings))
        self.status_posted_to_server_ts = time.time()


    ## REST API part of the server connection

    def get_linked_printer(self):
        resp = self.send_http_request('GET', '/api/v1/octo/printer/', raise_exception=True)
        return resp.json()['printer']


    def post_printer_event_to_server(self, event_title, event_text, event_type='PRINTER_ERROR', event_class='ERROR', attach_snapshot=False, **kwargs):
        event_data = dict(event_title=event_title, event_text=event_text, event_type=event_type, event_class=event_class, **kwargs)
        self.send_ws_msg_to_server({'passthru': {'printer_event': event_data}})

        # We dont' want to bombard the server with repeated events. So we keep track of the events sent since last restart.
        # However, there are probably situations in the future repeated events do need to be propagated to the server.
        if event_title in self.printer_events_posted:
            return

        self.printer_events_posted.append(event_title)

        files = None
        if attach_snapshot:
            try:
                files = {'snapshot': capture_jpeg(self.config.primary_webcam_config)}
            except Exception as e:
                _logger.warn('Failed to capture jpeg - ' + str(e))
                pass
        resp = self.send_http_request('POST', '/api/v1/octo/printer_events/', timeout=60, raise_exception=True, files=files, data=event_data)

    def post_pic_to_server(self, webcam_config, viewing_boost=False):
        if not webcam_config:
            _logger.warn('webcam_config is None. Skipping jpeg posting to server. Ill-configured [webcam] section?')
            return

        try:
            files = {'pic': capture_jpeg(webcam_config)}

            data = dict(
                is_primary_camera=webcam_config.is_primary_camera,
                is_nozzle_camera=webcam_config.is_nozzle_camera,
                camera_name=webcam_config.name,
                viewing_boost=viewing_boost
            )
            resp = self.send_http_request('POST', '/api/v1/octo/pic/', timeout=60, files=files, data=data, raise_exception=True, skip_debug_logging=True)
            _logger.debug('Jpeg posted to server - camera name: {} - viewing_boost: {} - {}'.format(webcam_config.name, viewing_boost, resp))
        except (URLError, HTTPError, requests.exceptions.RequestException, ValueError) as e:
            _logger.warn('Failed to capture jpeg - ' + str(e))
            return

    def send_http_request(self, method, uri, timeout=10, raise_exception=True, skip_debug_logging=False, **kwargs):
        endpoint = self.config.server.canonical_endpoint_prefix() + uri
        headers = {
            'Authorization': f'Token {self.config.server.auth_token}'
        }
        headers.update(kwargs.pop('headers', {}))

        _kwargs = dict(allow_redirects=True)
        _kwargs.update(kwargs)

        try:
            resp = requests.request(
                method, endpoint, timeout=timeout, headers=headers, **_kwargs)
            _logger.debug(f'{resp.request.method} {resp.url} - {resp.status_code}')

            if not skip_debug_logging:
                _logger.debug(curlify.to_curl(resp.request))
        except Exception:
            if raise_exception:
                raise
            return None

        if raise_exception:
            resp.raise_for_status()

        return resp
