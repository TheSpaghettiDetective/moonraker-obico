import time
import logging
import platform
import uuid
import io
import json
import socket
import requests
from requests.exceptions import HTTPError
import random
import string
import flask
from flask import request, jsonify
from werkzeug.serving import make_server
import argparse
from queue import Queue

from .version import VERSION
from .utils import raise_for_status, run_in_thread, verify_link_code, wait_for_port
from .config import Config

try:
    from secrets import token_hex
except ImportError:
    def token_hex(n):
        letters = string.ascii_letters + string.digits
        return "".join([random.choice(letters) for i in range(n)])

import netaddr.ip

_logger = logging.getLogger('obico.printer_discovery')

# we count steps instead of tracking timestamps;
# timestamps happened to be unreliable on rpi-s (NTP issue?)
# printer remains discoverable for about 100 minutes, give or take.
POLL_PERIOD = 5
MAX_POLLS = 1200
TOTAL_STEPS = POLL_PERIOD * MAX_POLLS

MAX_BACKOFF_SECS = 30


class PrinterDiscovery(object):

    def __init__(self, config, sentry):
        self.config = config
        self.sentry = sentry
        self.stopped = False
        self.device_secret = None
        self.static_info = {}

        # device_id is different every time plugin starts
        self.device_id = uuid.uuid4().hex  # type: str

    def start_and_block(self):
        _logger.info(
            'printer_discovery started, device_id: {}'.format(self.device_id))

        try:
            self._start()
        except Exception:
            self.stop()
            self.sentry.captureException()

        _logger.debug('printer_discovery quits')

    def _start(self):
        self.device_secret = token_hex(32)
        steps_remaining = TOTAL_STEPS

        host_or_ip = get_local_ip()

        sbc_model = ''
        try:
            sbc_mode = read('/proc/device-tree/model')[:253]
        except:
            pass

        self.static_info = dict(
            device_id=self.device_id,
            hostname=platform.uname()[1][:253],
            host_or_ip=host_or_ip,
            port=get_port(),
            os=get_os()[:253],
            arch=platform.uname()[4][:253],
            rpi_model=sbc_model,
            plugin_version=VERSION,
            agent='Obico for Klipper',
        )

        if not host_or_ip:
            _logger.info('printer_discovery could not find out local ip')
            self.stop()
            return

        run_in_thread(self.listen_to_handshake)

        while not self.stopped:

            self.config.load_from_config_file() # Refresh the config in case the token is obtained manually, or by the 6-digit method
            if self.config.server.auth_token:
                _logger.info('printer_discovery detected a configuration')
                self.stop()
                break

            steps_remaining -= 1
            if steps_remaining < 0:
                _logger.info('printer_discovery got deadline reached')
                self.stop()
                break

            try:
                if steps_remaining % POLL_PERIOD == 0:
                    self._call()
            except (IOError, OSError) as ex:
                # trying to catch only network related errors here,
                # all other errors must bubble up.

                # http4xx can be an actionable bug, let it bubble up
                if isinstance(ex, HTTPError):
                    status_code = ex.response.status_code
                    if 400 <= status_code < 500:
                        raise

            time.sleep(1)

    def stop(self):
        self.stopped = True
        _logger.info('printer_discovery is stopping')
        try:
            wait_for_port('127.0.0.1', get_port())  # Wait for Flask to start running. Otherwise we will get connection refused when trying to post to '/shutdown'
            requests.post(f'http://127.0.0.1:{get_port()}/shutdown')
        except Exception:
            pass

    def id_for_secret(self):

        def get_remote_address(request):
            forwardedFor = request.headers.get('X-Forwarded-For')
            if forwardedFor:
                return forwardedFor.split(',')[0]
            return request.remote_addr

        if (
            self.device_secret and
            is_local_address(
                get_remote_address(flask.request)
            ) and
            flask.request.args.get('device_id') == self.device_id
        ):
            accept = flask.request.headers.get('Accept', '')
            if 'application/json' in accept:
                resp = flask.Response(
                    json.dumps(
                        {'device_secret': self.device_secret}
                    ),
                    mimetype='application/json'
                )
            else:
                # Create an HTML response from a string
                resp_content = f"""
<html>
<body>
<p>Handshake succeeded!</p>
<p>You can close this window now.</p>
<script>
  console.log('called');
  window.opener.postMessage({{"device_secret": "{self.device_secret}"}}, '*');
</script>
</body>
</html>
"""
                resp = flask.make_response(resp_content)
                resp.headers['Content-Type'] = 'text/html'

            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Methods'] =\
                'GET, HEAD, OPTIONS'
            return resp

        return flask.abort(403)

    def _call(self):
        _logger.debug('printer_discovery calls server')
        data = self._collect_device_info()
        endpoint = self.config.server.canonical_endpoint_prefix() + '/api/v1/octo/unlinked/'
        resp = requests.request('POST', endpoint, timeout=5, data=json.dumps(data), headers={'Content-Type': 'application/json'})
        resp.raise_for_status()
        data = resp.json()
        for msg in data['messages']:
            self._process_message(msg)

    def _process_message(self, msg):
        # Stops after first verify attempt
        _logger.info('printer_discovery got incoming msg: {}'.format(msg))

        if msg['type'] == 'verify_code':
            self.config.load_from_config_file() # Refresh the config in case the token is obtained manually, or by the 6-digit method
            if self.config.server.auth_token:
                _logger.info('printer_discovery detected a configuration')
                self.stop()
                return

            if (
                not self.device_secret or
                'secret' not in msg['data'] or
                msg['data']['secret'] != self.device_secret
            ):
                _logger.error('printer_discovery got unmatching secret')
                self.sentry.captureMessage(
                    'printer_discovery got unmatching secret',
                    extra={'secret': self.device_secret, 'msg': msg}
                )
                self.stop()
                return

            if msg['device_id'] != self.device_id:
                _logger.error('printer_discovery got unmatching device_id')
                self.sentry.captureMessage(
                    'printer_discovery got unmatching device_id',
                    extra={'device_id': self.device_id, 'msg': msg}
                )
                self.stop()
                return

            code = msg['data']['code']
            verify_link_code(self.config, code)
        else:
            _logger.error('printer_discovery got unexpected message')

        self.stop()
        return

    def _collect_device_info(self):
        info = dict(**self.static_info)
        info['printerprofile'] = 'Unknown'
        info['machine_type'] = 'Klipper'
        return info

    def listen_to_handshake(self):
        handshake_app = flask.Flask('handshake')

        @handshake_app.route('/plugin/obico/grab-discovery-secret')
        def grab_discovery_secret():
            return self.id_for_secret()

        @handshake_app.route('/shutdown', methods=['POST'])
        def shutdown():
            q.put('Apparently and understandably flask has made it extremely difficult for developers to shut it down.')
            return 'Ok'

        # https://stackoverflow.com/questions/68885585/wait-for-value-then-stop-server-after-werkzeug-server-shutdown-is-deprecated
        q = Queue()
        handshake_server = make_server('0.0.0.0', get_port(), handshake_app)
        t = run_in_thread(handshake_server.serve_forever)
        q.get(block=True)
        handshake_server.shutdown()
        t.join()


def get_os():  # type: () -> str
    return ''


def read(path):  # type: (str) -> str
    try:
        with io.open(path, 'rt', encoding='utf8') as f:
            return f.readline().strip('\0').strip()
    except Exception:
        return ''


def _get_ip_addr():  # type () -> str
    primary_ip = ''
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    try:
        s.connect(('10.255.255.255', 1))
        primary_ip = s.getsockname()[0]
        s.close()
    except Exception:
        try:
            # None of these 2 ways are 100%. Double them to maximize the chance
            s.connect(('8.8.8.8', 53))
            primary_ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass

    return primary_ip


def get_port():
    return 8623


def get_local_ip():
    ip = _get_ip_addr()
    if ip and is_local_address(ip):
        return ip

    return ''


def is_local_address(address):
    try:
        ip = netaddr.IPAddress(address)
        return ip.is_private() or ip.is_loopback()
    except Exception as exc:
        _logger.error(
            'could not determine whether {} is local address ({})'.format(
                address, exc)
        )
        return False

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-c', '--config', dest='config_path', required=True,
        help='Path to config file (ini)'
    )
    args = parser.parse_args()
    config = Config(args.config_path)

    discovery = PrinterDiscovery(config=config.server)
    discovery.start_and_block()
