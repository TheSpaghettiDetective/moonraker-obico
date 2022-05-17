from typing import Dict
import requests  # type: ignore

from .logger import getLogger
from .wsconn import WSConn, ConnHandler
from .utils import Event

class ServerConn(ConnHandler):
    max_backoff_secs = 300
    flow_step_timeout_msecs = 5000

    def __init__(self, id, sentry, tsd_config, on_event):
        super().__init__(id, sentry, on_event)
        self.config: ServerConfig = tsd_config

    def flow(self):
        self.timer.reset(None)
        self.ready = False

        if self.conn:
            self.conn.close()

        self.logger.debug('fetching printer data')
        linked_printer = self._get_linked_printer()
        self.on_event(
            Event(sender=self.id, name='linked_printer', data=linked_printer)
        )

        self.conn = WSConn(
            id=self.id,
            auth_header_fmt='authorization: bearer {}',
            sentry=self.sentry,
            url=self.config.ws_url(),
            token=self.config.auth_token,
            on_event=self.push_event,
            logger=getLogger(f'{self.id}.ws'),
        )

        self.conn.start()

        self.logger.debug('waiting for connection')
        self.wait_for(self._received_connected)

        self.set_ready()
        self.logger.info('connection is ready')
        self.on_event(
            Event(sender=self.id, name=f'{self.id}_ready', data={})
        )

        self.loop_forever(self.on_event)

    def _get_linked_printer(self):
        if not self.config.auth_token:
            raise FlowError('auth_token not configured')

        try:
            resp = self.send_http_request(
                'GET',
                '/api/v1/octo/printer/',
            )
        except Exception as exc:
            raise FlowError('failed to fetch printer', exc=exc)

        return resp.json()['printer']

    def _received_connected(self, event):
        if event.name == 'connected':
            return True

    def send_status_update(self, data):
        if self.ready:
            self.conn.send(data)

    def send_http_request(
        self, method, uri, timeout=10, raise_exception=True,
        **kwargs
    ):
        endpoint = self.config.canonical_endpoint_prefix() + uri
        headers = {
            'Authorization': f'Token {self.config.auth_token}'
        }
        headers.update(kwargs.pop('headers', {}))

        _kwargs = dict(allow_redirects=True)
        _kwargs.update(kwargs)

        self.logger.debug(f'{method} {endpoint}')
        try:
            resp = requests.request(
                method, endpoint, timeout=timeout, headers=headers, **_kwargs)
        except Exception:
            if raise_exception:
                raise
            return None

        if raise_exception:
            # if resp.status_code in (401, 403):
            #     raise AuthenticationError(
            #             f'HTTP {resp.status_code}',
            #             exc=resp_to_exception(resp))
            resp.raise_for_status()

        return resp

    def send_passthru(self, payload: Dict):
        if self.ready:
            self.conn.send({'passthru': payload})

