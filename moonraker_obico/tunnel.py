import requests
import pickle
import logging
import threading
import time
import os
import zlib
from urllib.parse import urljoin

from .ws import WebSocketClient

COMPRESS_THRESHOLD = 1000

_logger = logging.getLogger('obico.app.tunnel')


class LocalTunnel(object):
    """
        Copied from Octoprint-Obico plugin source.
        Removed py2 and tunnel-v1 related parts.
    """

    def __init__(self, tunnel_config, on_http_response, on_ws_message, sentry):
        self.base_url = ('https://' if tunnel_config.dest_is_ssl else 'http://') + \
                tunnel_config.dest_host + \
                '' if tunnel_config.dest_port == '80' else tunnel_config.dest_port
        self.config = tunnel_config
        self.on_http_response = on_http_response
        self.on_ws_message = on_ws_message
        self.sentry = sentry
        self.ref_to_ws = {}
        self.request_session = requests.Session()

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
            _logger.info("Tunneled WS is closing")
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
        headers['Accept-Encoding'] = 'identity'

        _logger.debug('Tunneling (v2) "{}"'.format(url))

        resp_data = None
        if any([ (u in url) for u in self.config.url_blacklist]):
            resp_data = {
                'status': 404,
                'content': 'Blacklisted',
                'headers': {}
            }

        try:
            if not resp_data:
                resp = getattr(requests, method)(
                    url,
                    params=params,
                    headers={k: v for k, v in headers.items()},
                    data=data,
                    timeout=timeout,
                    allow_redirects=False) # The redirect should happen in the browser, not the plugin. Otherwise it causes tricky problems.

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
