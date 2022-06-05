from __future__ import absolute_import
from typing import Dict, Optional
import queue
import dataclasses
import threading
import json
import bson
import websocket
import logging

from .utils import ShutdownException, FatalError, ExpoBackoff

_logger = logging.getLogger('obico.wsconn')

@dataclasses.dataclass
class Event:
    name: str
    data: Dict
    sender: Optional[str] = None


class WSConn(object):

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


