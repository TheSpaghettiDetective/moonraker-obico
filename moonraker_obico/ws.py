# coding=utf-8

import time
import websocket
import logging
import threading
import inspect
import sys

_logger = logging.getLogger('obico.ws')

class WebSocketConnectionException(Exception):
    pass

class WebSocketClient:

    def __init__(self, url, header=None, on_ws_msg=None, on_ws_close=None, on_ws_open=None, subprotocols=None, waitsecs=120):
        self._mutex = threading.RLock()

        def on_error(ws, error):
            _logger.warning('Server WS ERROR: {}'.format(error))

            def run(*args):
                self.close()

            # https://websocket-client.readthedocs.io/en/latest/threading.html
            threading.Thread(target=run).start()

        def on_message(ws, msg):
            if on_ws_msg:
                on_ws_msg(ws, msg)

        def on_close(ws, close_status_code, close_msg):
            _logger.warning(f'WS Closed - {close_status_code} - {close_msg}')
            if on_ws_close:
                on_ws_close(ws, close_status_code=close_status_code)

        def on_open(ws):
            _logger.debug('WS Opened')

            def run(*args):
                if on_ws_open:
                    on_ws_open(ws)

            # https://websocket-client.readthedocs.io/en/latest/threading.html
            threading.Thread(target=run).start()

        _logger.debug('Connecting to websocket: {}'.format(url))
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

        wst = threading.Thread(target=self.ws.run_forever, kwargs=run_forever_kwargs)
        wst.daemon = True
        wst.start()

        for i in range(waitsecs * 10):  # Give it up to 120s for ws hand-shaking to finish
            if self.connected():
                return
            time.sleep(0.1)
        self.ws.close()
        raise WebSocketConnectionException('Not connected to websocket server after {}s'.format(waitsecs))

    def send(self, data, as_binary=False):
        with self._mutex:
            if self.connected():
                if as_binary:
                    self.ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
                else:
                    self.ws.send(data)

    def connected(self):
        with self._mutex:
            return self.ws.sock and self.ws.sock.connected

    def close(self):
        with self._mutex:
            self.ws.keep_running = False
            self.ws.close()

if __name__ == "__main__":
    import yaml
    import sys

    def on_msg(ws, msg):
        print(msg)

    def on_close(ws):
        print('Closed')

    with open(sys.argv[1]) as stream:
        config = yaml.load(stream.read()).get('plugins', {}).get('obico', {})

    url = config.get('endpoint_prefix', 'https://app.obico.io').replace('http', 'ws') + '/ws/dev/'
    token = config.get('auth_token')
    print('Connecting to:\n{}\nwith token:\n{}\n'.format(url, token))
    websocket.enableTrace(True)
    header = ["authorization: bearer " + token] if token else None
    ws = WebSocketClient(url, header=header, on_ws_msg=on_msg, on_ws_close=on_close)
    time.sleep(1)
    ws.close()
    time.sleep(1)
