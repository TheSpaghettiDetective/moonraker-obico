from typing import Optional, Dict, List, Tuple
import requests  # type: ignore
import logging
import time
import backoff
import queue
import bson
import json
import threading

from .utils import ExpoBackoff, get_tags
from .ws import WebSocketClient, WebSocketConnectionException
from .config import Config
from .printer import PrinterState

POST_STATUS_INTERVAL_SECONDS = 50.0

_logger = logging.getLogger('obico.server_conn')

class ServerConn:

    def __init__(self, config: Config, printer_state: PrinterState, process_server_msg, sentry):
        self.config: Config = config
        self.printer_state: PrinterState() = printer_state
        self.process_server_msg = process_server_msg
        self.sentry = sentry

        self.status_posted_to_server_ts = 0
        self.ss = None
        self.message_queue_to_server = queue.Queue(maxsize=1000)
        self.status_update_booster = 0    # update status at higher frequency when self.status_update_booster > 0


    ## WebSocket part of the server connection

    def start(self):
        thread = threading.Thread(target=self.message_to_server_loop)
        thread.daemon = True
        thread.start()

        while True:
            try:
                interval_in_seconds = POST_STATUS_INTERVAL_SECONDS
                if self.status_update_booster > 0:
                    interval_in_seconds /= 5

                if self.status_posted_to_server_ts < time.time() - interval_in_seconds:
                    self.post_status_update_to_server(config=self.config)

            except Exception as e:
                self.sentry.captureException(tags=get_tags())

            time.sleep(1)


    def message_to_server_loop(self):

        def on_server_ws_close(ws):
            if self.ss and self.ss.ws and self.ss.ws == ws:
                self.ss = None

        def on_server_ws_open(ws):
            if self.ss and self.ss.ws and self.ss.ws == ws:
                self.post_status_update_to_server() # Make sure an update is sent asap so that the server can rely on the availability of essential info such as agent.version

        def on_message(ws, msg):
            self.process_server_msg(json.loads(msg))

        server_ws_backoff = ExpoBackoff(300)
        while True:
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
                    _logger.debug("Sending binary ({} bytes) to server".format(len(raw)))
                else:
                    _logger.debug("Sending to server: \n{}".format(data))
                    raw = json.dumps(data, default=str)
                self.ss.send(raw, as_binary=as_binary)
                server_ws_backoff.reset()
            except WebSocketConnectionException as e:
                _logger.warning(e)
                server_ws_backoff.more(e)
            except Exception as e:
                self.sentry.captureException(tags=get_tags())
                server_ws_backoff.more(e)

    def send_ws_msg_to_server(self, data, as_binary=False):
        try:
            self.message_queue_to_server.put_nowait((data, as_binary))
        except queue.Full:
            _logger.warning("Server message queue is full, msg dropped")

    def post_status_update_to_server(self, print_event: Optional[str] = None,  config: Optional[Config] = None):
        self.send_ws_msg_to_server(self.printer_state.to_dict(print_event=print_event, config=config))
        self.status_posted_to_server_ts = time.time()


    ## REST API part of the server connection

    @backoff.on_predicate(backoff.expo, max_value=1200)
    def get_linked_printer(self):
        if not self.config.server.auth_token:
            raise Exception('auth_token not configured. Exiting the process...')

        try:
            resp = self.send_http_request('GET', '/api/v1/octo/printer/', raise_exception=True)
        except Exception:
            return None  # Triggers a backoff

        printer = resp.json()['printer']
        _logger.info('Linked printer: {}'.format(printer))

        return printer


    def send_http_request(
        self, method, uri, timeout=10, raise_exception=True,
        **kwargs
    ):
        endpoint = self.config.server.canonical_endpoint_prefix() + uri
        headers = {
            'Authorization': f'Token {self.config.server.auth_token}'
        }
        headers.update(kwargs.pop('headers', {}))

        _kwargs = dict(allow_redirects=True)
        _kwargs.update(kwargs)

        _logger.debug(f'{method} {endpoint}')
        try:
            resp = requests.request(
                method, endpoint, timeout=timeout, headers=headers, **_kwargs)
        except Exception:
            if raise_exception:
                raise
            return None

        if raise_exception:
            resp.raise_for_status()

        return resp
