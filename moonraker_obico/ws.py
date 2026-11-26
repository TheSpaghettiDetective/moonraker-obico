# coding=utf-8

import time
import websocket
import logging
import threading
import inspect
import socket
import sys

from .redaction import REDACTED, redact_text, redact_url
from .logger import install_redacting_filter

_logger = logging.getLogger('obico.ws')

class WebSocketConnectionException(Exception):
    pass

class WebSocketClient:

    def __init__(self, url, header=None, on_ws_msg=None, on_ws_close=None, on_ws_open=None, subprotocols=None, waitsecs=120,
                 ping_interval=0, ping_timeout=None):
        """ping_interval/ping_timeout (seconds): when ping_interval > 0, send a websocket ping that often and
        close the connection if no pong arrives within ping_timeout. This is what detects a connection that died
        without a FIN or RST (firewall reboot, NAT state flush, Wi-Fi roam). Without it the socket keeps reporting
        connected() and the caller never reconnects. Off by default to keep existing behavior for local connections.
        """
        if ping_interval and (not ping_timeout or ping_timeout >= ping_interval):
            raise ValueError('ping_timeout must be > 0 and less than ping_interval')

        self._mutex = threading.RLock()

        def on_error(ws, error):
            _logger.warning('Server WS ERROR: {}'.format(redact_text(error)))

            def run(*args):
                self.close()

            # https://websocket-client.readthedocs.io/en/latest/threading.html
            threading.Thread(target=run).start()

        def on_message(ws, msg):
            if on_ws_msg:
                on_ws_msg(ws, msg)

        def on_close(ws, close_status_code, close_msg):
            _logger.warning('WS Closed - {} - {}'.format(close_status_code, redact_text(close_msg)))
            if on_ws_close:
                on_ws_close(ws, close_status_code=close_status_code)

        def on_open(ws):
            _logger.debug('WS Opened')

            def run(*args):
                if on_ws_open:
                    on_ws_open(ws)

            # https://websocket-client.readthedocs.io/en/latest/threading.html
            threading.Thread(target=run).start()

        _logger.debug('Connecting to websocket: {}'.format(redact_url(url)))
        self.ws = websocket.WebSocketApp(
            url,
            on_message=on_message,
            on_open=on_open,
            on_close=on_close,
            #on_error=on_error,
            header=header,
            subprotocols=subprotocols
        )

        # Websocket-client has changed their behavior on reconnecting. The latest version allows global
        # setting on reconnecting interval. Let's disable that behavior to make it consistent with the older version.

        if sys.version_info >= (3, 0):
            run_forever_kwargs = {'reconnect': 0} if 'reconnect' in inspect.getfullargspec(websocket.WebSocketApp.run_forever).args else {}
        else:
            run_forever_kwargs = {'reconnect': 0} if 'reconnect' in inspect.getargspec(websocket.WebSocketApp.run_forever) else {}

        if ping_interval:
            run_forever_kwargs.update(
                ping_interval=ping_interval,
                ping_timeout=ping_timeout,
                # Bound how long a write can sit unacknowledged on a dead link (Linux only), so a blocked
                # send() fails instead of hanging for the ~15 min kernel retransmission default.
                sockopt=self._dead_peer_sockopts(ping_interval + ping_timeout),
            )

        wst = threading.Thread(target=self.ws.run_forever, kwargs=run_forever_kwargs)
        wst.daemon = True
        wst.start()

        for i in range(waitsecs * 10):  # Give it up to 120s for ws hand-shaking to finish
            if self.connected():
                return
            time.sleep(0.1)
        self.ws.close()
        raise WebSocketConnectionException('Not connected to websocket server after {}s'.format(waitsecs))

    @staticmethod
    def _dead_peer_sockopts(timeout_secs):
        opts = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
        if hasattr(socket, 'TCP_USER_TIMEOUT'):
            opts.append((socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, int(timeout_secs * 1000)))
        return tuple(opts)

    def send(self, data, as_binary=False):
        with self._mutex:
            if self.connected():
                if as_binary:
                    self.ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
                else:
                    self.ws.send(data)

    def connected(self):
        with self._mutex:
            sock = self.ws.sock
            return sock is not None and sock.connected

    def close(self):
        with self._mutex:
            self.ws.keep_running = False
            self.ws.close()

if __name__ == "__main__":
    import yaml
    import sys

    def on_msg(ws, msg):
        print(redact_text(msg))

    def on_close(ws):
        print('Closed')

    with open(sys.argv[1]) as stream:
        config = yaml.load(stream.read()).get('plugins', {}).get('obico', {})

    url = config.get('endpoint_prefix', 'https://app.obico.io').replace('http', 'ws') + '/ws/dev/'
    token = config.get('auth_token')
    print('Connecting to:\n{}\nwith token:\n{}\n'.format(redact_url(url), REDACTED if token else None))
    websocket.enableTrace(True)
    install_redacting_filter(logging.getLogger('websocket'))
    header = ["authorization: bearer " + token] if token else None
    ws = WebSocketClient(url, header=header, on_ws_msg=on_msg, on_ws_close=on_close)
    time.sleep(1)
    ws.close()
    time.sleep(1)
