import requests
import pickle
import logging
import threading
import time
import os
import sys
import zlib
try:
    from urllib.parse import urljoin
except ImportError:
    from urlparse import urljoin

from .ws import WebSocketClient

WRITE_MODE = 'w' if sys.version_info[0] < 3 else 'wb'
READ_MODE = 'r' if sys.version_info[0] < 3 else 'rb'
COMPRESS_THRESHOLD = 1000

_logger = logging.getLogger('octoprint.plugins.obico')


class LocalTunnel(object):

    def __init__(self, base_url, on_http_response, on_ws_message, data_dir, sentry):
        self.base_url = base_url
        self.on_http_response = on_http_response
        self.on_ws_message = on_ws_message
        self.sentry = sentry
        self.ref_to_ws = {}
        self.cj_path = os.path.join(data_dir, '.tunnel.cj.pickled')
        self.request_session = requests.Session()
        try:
            with open(self.cj_path, READ_MODE) as fp:
                jar = pickle.load(fp)
                if isinstance(jar, requests.cookies.RequestsCookieJar):
                    self.request_session.cookies = jar
        except:
            pass   # Start with a clean session without cookies if cookie jar loading fails for any reason

    def send_http_to_local(
            self, ref, method, path,
            params=None, data=None, headers=None, timeout=30):

        url = urljoin(self.base_url, path)

        _logger.debug('Tunneling "{}"'.format(url))
        try:
            resp = getattr(self.request_session, method)(
                url,
                params=params,
                headers={k: v for k, v in headers.items() if k != 'Cookie'},
                data=data,
                timeout=timeout,
                allow_redirects=False)

            save_cookies = False
            if resp.status_code == 403:      # failed to authenticate
                self.request_session.cookies.clear()
                save_cookies = True

            if resp.headers.pop('Set-Cookie', None) or save_cookies: # Stop set-cookie from being propagated to Obico Server
                with open(self.cj_path, WRITE_MODE) as fp:
                    pickle.dump(self.request_session.cookies, fp)

            compress = len(resp.content) >= COMPRESS_THRESHOLD
            resp_data = {
                'status': resp.status_code,
                'compressed': compress,
                'content': zlib.compress(resp.content) if compress else resp.content,
                'headers': {k: v for k, v in resp.headers.items()},
            }
        except Exception as ex:
            resp_data = {
                'status': 502,
                'content': repr(ex),
                'headers': {}
            }

        self.on_http_response(
            {'http.tunnel': {'ref': ref, 'response': resp_data}},
            as_binary=True)
        return

    def send_ws_to_local(self, ref, path, data, type_):
        ws = self.ref_to_ws.get(ref, None)

        if type_ == 'tunnel_close':
            if ws is not None:
                ws.close()
            return

        if ws is None:
            self.connect_octoprint_ws(ref, path)
            time.sleep(1)  # Wait to make sure websocket is established before `send` is called

        if data is not None:
            ws.send(data)

    def connect_octoprint_ws(self, ref, path):
        def on_ws_close(ws, **kwargs):
            _logger.info("OctoPrint WS is closing")
            if ref in self.ref_to_ws:
                del self.ref_to_ws[ref]     # Remove octoprint ws from refs as on_ws_message may fail
                self.on_ws_message(
                    {'ws.tunnel': {'ref': ref, 'data': None, 'type': 'octoprint_close'}},
                    as_binary=True)

        def on_ws_msg(ws, data):
            try:
                self.on_ws_message(
                    {'ws.tunnel': {'ref': ref, 'data': data, 'type': 'octoprint_message'}},
                    as_binary=True)
            except:
                self.sentry.captureException()
                ws.close()

        url = urljoin(self.base_url, path)
        url = url.replace('http://', 'ws://')
        url = url.replace('https://', 'wss://')

        ws = WebSocketClient(
            url,
            token=None,
            on_ws_msg=on_ws_msg,
            on_ws_close=on_ws_close,
        )
        self.ref_to_ws[ref] = ws

    def close_all_octoprint_ws(self):
        for ref, ws in self.ref_to_ws.items():
            ws.close()

    def send_http_to_local_v2(
            self, ref, method, path,
            params=None, data=None, headers=None, timeout=30):

        url = urljoin(self.base_url, path)

        _logger.debug('Tunneling (v2) "{}"'.format(url))
        try:
            resp = getattr(requests, method)(
                url,
                params=params,
                headers={k: v for k, v in headers.items()},
                data=data,
                timeout=timeout,
                allow_redirects=False) # The redirect should happen in the browser, not the plugin. Otherwise it causes tricky problems.

            if sys.version_info[0] < 3:
                cookies = [
                    h[len('set-cookie:'):].strip()
                    for h in resp.raw._original_response.msg.headers
                    if h.lower().startswith('set-cookie')
                ]
            else:
                cookies = resp.raw._original_response.msg.get_all('Set-Cookie')

            compress = len(resp.content) >= COMPRESS_THRESHOLD
            resp_data = {
                'status': resp.status_code,
                'compressed': compress,
                'content': (
                    zlib.compress(resp.content)
                    if compress
                    else resp.content
                ),
                'cookies': cookies,
                'headers': {k: v for k, v in resp.headers.items()},
            }
        except Exception as ex:
            resp_data = {
                'status': 502,
                'content': repr(ex),
                'headers': {}
            }

        self.on_http_response(
            {'http.tunnelv2': {'ref': ref, 'response': resp_data}},
            as_binary=True)
        return
