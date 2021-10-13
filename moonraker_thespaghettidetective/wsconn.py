from __future__ import absolute_import
import queue
import threading
import json
import bson
import websocket
import time

from .logger import getLogger
from .utils import (
    Event, FlowTimeout, ShutdownException,
    FlowError, FatalError, ExpoBackoff)

logger = getLogger('utils')


class WSConn(object):

    def __init__(
        self, name, sentry, url, token, on_event, auth_header_fmt,
        subprotocols=None, logger=logger
    ):
        self.shutdown = False
        self.id = name
        self.sentry = sentry
        self.url = url
        self.token = token
        self._on_event = on_event
        self.to_server_q = queue.Queue(maxsize=1000)
        self.wsock = None
        self.auth_header_fmt = auth_header_fmt
        self.subprotocols = subprotocols
        self.logger = logger

    def send(self, data, is_binary=False):
        try:
            self.to_server_q.put_nowait((data, is_binary, False))
        except queue.Full:
            self.logger.exception('sending queue is full')

    def on_event(self, event):
        if self.shutdown:
            return

        self._on_event(event)

    def close(self):
        self.shutdown = True
        try:
            self.to_server_q.put_nowait((None, False, True))
        except queue.Full:
            self.logger.exception('sending queue is full')

    def start(self):
        server_thread = threading.Thread(
            target=self.sender_loop)
        server_thread.daemon = True
        server_thread.start()

    def _connect_websocket(self):
        def on_ws_error(ws, error):
            self.logger.debug(f'connection error ({error})')
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
            self.logger.debug('connection closed')
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
            self.logger.debug(f'receiving {raw}')
            try:
                self.on_event(
                    Event(
                        sender=self.id, name='message', data=json.loads(raw)
                    )
                )
            except queue.Full:
                self.sentry.captureException(with_tags=True)

        self.logger.info(f'connecting to {self.url}')
        self.wsock = websocket.WebSocketApp(
            self.url,
            on_message=on_ws_message,
            on_open=on_ws_open,
            on_close=on_ws_close,
            on_error=on_ws_error,
            header=[self.auth_header_fmt.format(self.token), ],
            subprotocols=self.subprotocols
        )

        wst = threading.Thread(target=self.wsock.run_forever)
        wst.daemon = True
        wst.start()

    def sender_loop(self):
        try:
            self._connect_websocket()
            while self.shutdown is False:
                (data, as_binary, shutdown) = self.to_server_q.get()

                if shutdown:
                    self.shutdown = True
                    self.wsock.close()
                    continue

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
                    self.logger.debug(f'sending {raw}')
                    self.wsock.send(raw, opcode=opcode)
                else:
                    self.logger.error(f'unable to send {raw}')
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
            self.logger.warning(e)


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


class ConnHandler(object):
    max_backoff_secs: int = 300
    flow_step_timeout_msecs: int = 2000

    def __init__(self, name, sentry, on_event):
        self.name = name
        self.logger = getLogger(self.name)
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
            logger_=self.logger
        )

    def on_event(self, event):
        if self.shutdown:
            return

        self._on_event(event)

    def start(self):
        while self.shutdown is False:
            try:
                self.flow()
            except FlowError as err:
                self.logger.error(f'got error ({err}), reconnecting')
                self.reconn_backoff.more(err)
            except FlowTimeout as err:
                self.logger.error('got flow related timeout, reconnecting')
                self.reconn_backoff.more(err)
            except ShutdownException:
                self.logger.error('shutting down')
                break
            except FatalError as exc:
                self.logger.error(f'got fatal error ({exc})')
                self.on_event(
                    Event(
                        sender=self.name, name='fatal_error',
                        data={'exc': exc}
                    )
                )
                self.close()

    def set_ready(self):
        self.ready = True
        self.reconn_backoff.reset()

    def close(self):
        self.push_event(Event(name='shutdown', data={}))
        self.shutdown = True

    def push_event(self, event):
        if self.shutdown:
            self.logger.debug(f'is shutdown, dropping event {event}')
            return True

        try:
            self.q.put_nowait(event)
            return True
        except queue.Full:
            self.logger.error(f'event queue is full, dropping {event}')
            return False

    def wait_for(self, process_fn, timeout_msecs=-1, loop_forever=False):
        if timeout_msecs == -1:
            self.timer.reset(self.flow_step_timeout_msecs)
        else:
            self.timer.reset(timeout_msecs)

        while self.shutdown is False:
            event = self.q.get()

            # self.logger.debug(f'event: {event}')

            if self._wait_for(event, process_fn, timeout_msecs):
                if not loop_forever:
                    return

    def _wait_for(self, event, process_fn, timeout_msecs):
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
                raise FatalError(f'{self.name} failed to authenticate', exc)

            message = str(exc) if exc else 'connection error'
            raise FlowError(message, exc=exc)

        if event.name == 'disconnected':
            self.ready = False
            self.on_event(event)
            raise FlowError('diconnected')

        if (
            event.name == 'timeout' and
            event.data['timer_id'] == self.timer.id
        ):
            raise FlowTimeout('timed out')

        if process_fn(event):
            return True

        return None

    def prepare(self):
        raise NotImplementedError

    def flow(self):
        self.prepare()
        self.set_ready()
        self.wait_for(self.on_event, None, loop_forever=True)
