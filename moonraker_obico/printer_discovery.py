import time
import logging
import platform
import uuid
import io
import json
import socket
import requests
from threading import Lock
from requests.exceptions import HTTPError
import random
import string
import flask
from flask import request, jsonify
from werkzeug.serving import make_server
import argparse
from queue import Queue
import backoff

from .version import VERSION
from .utils import raise_for_status, run_in_thread, verify_link_code
from .config import Config
from .moonraker_conn import MoonrakerConn

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
POLL_PERIOD = 2
MAX_BACKOFF_SECS = 30

HANDSHAKE_PORT = 46793

class StubMoonrakerConn:
    """
    The only purpose of this.moonrakerconn is to set the OBICO_LINK_STATUS macro variables.

    This class is a stub for that purpose so that during situations like linking from console, the plugin doesn't crash.
    The only function sacrificed is set_macro_variables.
    """

    def __getattr__(self, name):
        def method(*args, **kwargs):
            _logger.debug(f"Stubbing {name} call")
        return method

class PrinterDiscovery(object):

    def __init__(self, config, sentry, moonrakerconn=None):
        self.moonrakerconn = moonrakerconn if moonrakerconn is not None else StubMoonrakerConn()
        self.config = config
        self.sentry = sentry
        self.stopped = False
        self.static_info = {}

        # One time passcode to share between the plugin and the server
        self.one_time_passcode = ''

        # The states for auto discovery handshake
        # device_id is different every time plugin starts
        self.device_id = uuid.uuid4().hex  # type: str
        self.device_secret = None

    def start_and_block(self, max_polls=7200):
        # printer remains discoverable for about 4 hours, give or take.
        total_steps = POLL_PERIOD * max_polls
        _logger.info(
            'printer_discovery started, device_id: {}'.format(self.device_id))

        self.set_obico_link_status(False, '', '')

        try:
            self._start(total_steps)
        except Exception:
            self.stop()
            self.sentry.captureException()

        _logger.debug('printer_discovery quits')

    def get_one_time_passcode(self):
        return self.one_time_passcode

    def set_one_time_passcode(self, code):
        self.one_time_passcode = code

    def set_obico_link_status(self, is_linked, one_time_passcode, one_time_passlink):
        self.moonrakerconn.set_macro_variables('OBICO_LINK_STATUS',
            is_linked=is_linked,
            one_time_passcode=f'\'"{one_time_passcode}"\'', # f'\'"{code}"\'' because of https://github.com/Klipper3d/klipper/issues/4816#issuecomment-950109507
            one_time_passlink=f'\'"{one_time_passlink}"\''
        )


    def _start(self, steps_remaining):
        self.device_secret = token_hex(32)

        sbc_model = ''
        try:
            sbc_mode = read('/proc/device-tree/model')[:253]
        except:
            pass

        self.static_info = dict(
            device_id=self.device_id,
            hostname=platform.uname()[1][:253],
            port=HANDSHAKE_PORT,
            os=get_os()[:253],
            arch=platform.uname()[4][:253],
            rpi_model=sbc_model,
            plugin_version=VERSION,
            agent='moonraker_obico',
        )

        printer_meta_data = self.config.get_meta_as_dict()
        if printer_meta_data:
            self.static_info['meta'] = printer_meta_data

        run_in_thread(self.listen_to_handshake)

        while not self.stopped:

            self.config.load_from_config_file() # Refresh the config in case the token is obtained manually, or by the 6-digit method
            if self.config.server.auth_token:
                _logger.info('printer_discovery detected a configuration')
                self.stop()
                break

            try:
                if steps_remaining % POLL_PERIOD == 0:

                    self.static_info['host_or_ip'] = get_local_ip()
                    resp = self.announce_unlinked_status()
                    resp.raise_for_status()
                    data = resp.json()

                    if self._process_one_time_passcode_response(data): # Verified. Stop discovery process
                        self.stop()
                        break

                    # Auto discovery handshake will result in a message from one of the calls.
                    self._process_unlinked_api_response(data)

            except (IOError, OSError) as ex:
                # Should continue on error in case of temporary network problems
                _logger.warning(ex)

            steps_remaining -= 1
            if steps_remaining < 0:
                _logger.info('printer_discovery got deadline reached')
                self.stop()
                break

            time.sleep(1)

    def stop(self):
        self.stopped = True
        _logger.info('printer_discovery is stopping')
        try:
            requests.post(f'http://127.0.0.1:{HANDSHAKE_PORT}/shutdown')
        except Exception:
            pass

    @backoff.on_exception(backoff.expo, Exception, max_value=120)
    def announce_unlinked_status(self):
        data = self._collect_device_info()

        data['one_time_passcode'] = self.get_one_time_passcode()

        endpoint = self.config.server.canonical_endpoint_prefix() + '/api/v1/octo/unlinked/'
        _logger.debug(f'calling {endpoint}')
        resp = requests.request('POST', endpoint, timeout=5, data=json.dumps(data), headers={'Content-Type': 'application/json'})
        _logger.debug(f'got response {resp.status_code} {resp.text}')
        return resp

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
        handshake_server = make_server('0.0.0.0', HANDSHAKE_PORT, handshake_app)
        t = run_in_thread(handshake_server.serve_forever)
        q.get(block=True)
        handshake_server.shutdown()
        t.join()

    # Return: True: one time passcode has a match and verified
    def _process_one_time_passcode_response(self, data):
        if 'one_time_passcode' not in data or 'verification_code' not in data:
            _logger.warning('No one_time_passcode or verification_code in response. Maybe old server version?')
            return False

        verification_code = data['verification_code']
        if verification_code != '': # Server tells us we got a match for one time passcode
            verify_link_code(self.config, verification_code)
            self.set_one_time_passcode('')
            self.set_obico_link_status(True, '', '')
            return True

        new_one_time_passcode = data['one_time_passcode']
        if self.get_one_time_passcode() != new_one_time_passcode:
            self.set_one_time_passcode(new_one_time_passcode)

        self.set_obico_link_status(False, new_one_time_passcode, data['one_time_passlink'])

        return False

    # A very convoluted way to
    #   1. verify the app is local
    #   2. give the app the secret to exchange for verification code,
    #   3. use verification code to exchange for auth token

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

    def _process_unlinked_api_response(self, data):
        # The response message was a very over-engineered way to send a single message. It's a list of one message.
        # The morale of the story is: don't over-engineer.
        if 'messages' not in data or not isinstance(data['messages'], list) or len(data['messages']) != 1:
            return

        msg = data['messages'][0]

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
                _logger.warning('printer_discovery got unmatching secret')
                self.sentry.captureMessage(
                    'printer_discovery got unmatching secret',
                    extra={'secret': self.device_secret, 'msg': msg}
                )
                self.stop()
                return

            if msg['device_id'] != self.device_id:
                _logger.warning('printer_discovery got unmatching device_id')
                self.sentry.captureMessage(
                    'printer_discovery got unmatching device_id',
                    extra={'device_id': self.device_id, 'msg': msg}
                )
                self.stop()
                return

            code = msg['data']['code']
            verify_link_code(self.config, code)
        else:
            _logger.warning('printer_discovery got unexpected message. Dropping it.')

        self.stop()
        return


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
        _logger.warning(
            'could not determine whether {} is local address ({})'.format(
                address, exc)
        )
        return False
