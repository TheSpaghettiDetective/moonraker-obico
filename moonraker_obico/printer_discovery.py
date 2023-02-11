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
import argparse

from .version import VERSION
from .utils import raise_for_status
from .config import Config

try:
    from secrets import token_hex
except ImportError:
    def token_hex(n):
        letters = string.ascii_letters + string.digits
        return "".join([random.choice(letters) for i in range(n)])

import netaddr.ip

_logger = logging.getLogger('octoprint.plugins.obico')

# we count steps instead of tracking timestamps;
# timestamps happened to be unreliable on rpi-s (NTP issue?)
# printer remains discoverable for about 10 minutes, give or take.
POLL_PERIOD = 5
MAX_POLLS = 120
TOTAL_STEPS = POLL_PERIOD * MAX_POLLS

MAX_BACKOFF_SECS = 30


class PrinterDiscovery(object):

    def __init__(self, config,):
        self.config = config
        self.stopped = False
        self.device_secret = None
        self.static_info = {}
        self.plugin = None

        # device_id is different every time plugin starts
        self.device_id = uuid.uuid4().hex  # type: str

    def start_and_block(self):
        _logger.info(
            'printer_discovery started, device_id: {}'.format(self.device_id))

        #try:
        self._start()
        #except Exception:
            #self.stop()

        _logger.debug('printer_discovery quits')

    def _start(self):
        self.device_secret = token_hex(32)
        steps_remaining = TOTAL_STEPS

        host_or_ip = get_local_ip(self.plugin)

        self.static_info = dict(
            device_id=self.device_id,
            hostname=platform.uname()[1][:253],
            host_or_ip=host_or_ip,
            port=get_port(self.plugin),
            os=get_os(),
            arch=platform.uname()[4][:253],
            rpi_model=read('/proc/device-tree/model')[:253],
            plugin_version=VERSION,
            agent='Obico for Klipper',
        )

        if not host_or_ip:
            _logger.info('printer_discovery could not find out local ip')
            self.stop()
            return

        while not self.stopped:
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

    def id_for_secret(self):

        def get_remote_address(request):
            forwardedFor = request.headers.get('X-Forwarded-For')
            if forwardedFor:
                return forwardedFor.split(',')[0]
            return request.remote_addr

        if (
            self.device_secret and
            is_local_address(
                self.plugin,
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
                resp = flask.Response(
                    flask.render_template(
                        'obico_discovery.jinja2',
                        device_secret=self.device_secret
                    )
                )
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Methods'] =\
                'GET, HEAD, OPTIONS'
            return resp

        return flask.abort(403)

    def _call(self):
        _logger.debug('printer_discovery calls server')
        data = self._collect_device_info()
        endpoint = self.config.canonical_endpoint_prefix() + '/api/v1/octo/unlinked/'
        resp = requests.request('POST', endpoint, timeout=5, data=json.dumps(data), headers={'Content-Type': 'application/json'})
        resp.raise_for_status()
        data = resp.json()
        for msg in data['messages']:
            self._process_message(msg)

    def _process_message(self, msg):
        # Stops after first verify attempt
        _logger.info('printer_discovery got incoming msg: {}'.format(msg))

        if msg['type'] == 'verify_code':
            if (
                not self.device_secret or
                'secret' not in msg['data'] or
                msg['data']['secret'] != self.device_secret
            ):
                _logger.error('printer_discovery got unmatching secret')
                self.stop()
                return

            if msg['device_id'] != self.device_id:
                _logger.error('printer_discovery got unmatching device_id')
                self.stop()
                return

            code = msg['data']['code']
            #result = verify_code(self.plugin, {'code': code})

#            if result['succeeded'] is True:
#                _logger.info('printer_discovery verified code successfully')
#                self.plugin._plugin_manager.send_plugin_message(
#                    self.plugin._identifier, {'printer_autolinked': True})
#            else:
#                _logger.error('printer_discovery could not verify code')
#                self.plugin.sentry.captureMessage(
#                    'printer_discovery could not verify code',
#                    extra={'code': code})
#
            self.stop()
            return

        _logger.error('printer_discovery got unexpected message')

    def _collect_device_info(self):
        info = dict(**self.static_info)
        info['printerprofile'] = 'Unknown'
        info['machine_type'] = 'Klipper'
        return info


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


def get_port(plugin):
    return 8623


def get_local_ip(plugin=None):
    ip = _get_ip_addr()
    if ip and is_local_address(plugin, ip):
        return ip

    return ''


def is_local_address(plugin, address):
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
