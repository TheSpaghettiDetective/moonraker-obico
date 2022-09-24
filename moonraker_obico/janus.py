import os
import logging
import subprocess
import time
from threading import Thread
import backoff
import json
import socket

from .utils import ExpoBackoff, pi_version, to_unicode
from .ws import WebSocketClient

_logger = logging.getLogger('obico.janus')

JANUS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'janus')
JANUS_SERVER = os.getenv('JANUS_SERVER', '127.0.0.1')
JANUS_WS_PORT = 8188
JANUS_DATA_PORT = 8009  # check streaming plugin config
MAX_PAYLOAD_SIZE = 1500  # hardcoded in streaming plugin


class JanusConn:

    def __init__(self, config, server_conn, sentry):
        self.config = config
        self.server_conn = server_conn
        self.sentry = sentry
        self.janus_ws_backoff = ExpoBackoff(120, max_attempts=20)
        self.janus_ws = None
        self.janus_proc = None
        self.shutting_down = False

    def start(self):

        if os.getenv('JANUS_SERVER', '').strip() != '':
            _logger.warning('Using an external Janus gateway. Not starting the built-in Janus gateway.')
            self.start_janus_ws()
            return

        if not pi_version():
            _logger.warning('No external Janus gateway. Not on a Pi. Skipping Janus connection.')
            return

        def ensure_janus_config():
            janus_conf_tmp = os.path.join(JANUS_DIR, 'etc/janus/janus.jcfg.template')
            janus_conf_path = os.path.join(JANUS_DIR, 'etc/janus/janus.jcfg')
            with open(janus_conf_tmp, "rt") as fin:
                with open(janus_conf_path, "wt") as fout:
                    for line in fin:
                        line = line.replace('{JANUS_HOME}', JANUS_DIR)
                        line = line.replace('{TURN_CREDENTIAL}', self.config.server.auth_token)
                        fout.write(line)

            video_enabled = 'false' if self.config.webcam.disable_video_streaming else 'true'
            streaming_conf_tmp = os.path.join(JANUS_DIR, 'etc/janus/janus.plugin.streaming.jcfg.template')
            streaming_conf_path = os.path.join(JANUS_DIR, 'etc/janus/janus.plugin.streaming.jcfg')
            with open(streaming_conf_tmp, "rt") as fin:
                with open(streaming_conf_path, "wt") as fout:
                    for line in fin:
                        line = line.replace('{VIDEO_ENABLED}', str(video_enabled))
                        fout.write(line)

        def run_janus_forever():

            @backoff.on_exception(backoff.expo, Exception, max_tries=5)
            def run_janus():
                janus_cmd = os.path.join(JANUS_DIR, 'run_janus.sh')
                _logger.debug('Popen: {}'.format(janus_cmd))
                self.janus_proc = subprocess.Popen(janus_cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

                while not self.shutting_down:
                    line = to_unicode(self.janus_proc.stdout.readline(), errors='replace')
                    if line:
                        _logger.debug('JANUS: ' + line.rstrip())
                    elif not self.shutting_down:  # line == None means the process quits
                        self.janus_proc.wait()
                        raise Exception('Janus quit! This should not happen. Exit code: {}'.format(self.janus_proc.returncode))

            try:
                run_janus()
            except Exception as ex:
                self.sentry.captureException()
                self.server_conn.post_printer_event_to_server(
                    'moonraker-obico: Webcam Streaming Failed',
                    'The webcam streaming failed to start. Obico is now streaming at 0.1 FPS.',
                    event_class='WARNING',
                    info_url='https://www.obico.io/docs/user-guides/webcam-stream-stuck-at-1-10-fps/',
                )

        ensure_janus_config()
        janus_proc_thread = Thread(target=run_janus_forever)
        janus_proc_thread.daemon = True
        janus_proc_thread.start()

        self.wait_for_janus()
        self.start_janus_ws()

    def pass_to_janus(self, msg):
        if self.janus_ws and self.janus_ws.connected():
            self.janus_ws.send(msg)

    @backoff.on_exception(backoff.expo, Exception, max_tries=10)
    def wait_for_janus(self):
        time.sleep(1)
        socket.socket().connect((JANUS_SERVER, JANUS_WS_PORT))

    def start_janus_ws(self):

        def on_close(ws, **kwargs):
            self.janus_ws_backoff.more(Exception('Janus WS connection closed!'))
            if not self.shutting_down:
                _logger.warning('Reconnecting to Janus WS.')
                self.start_janus_ws()

        def on_message(ws, msg):
            if self.process_janus_msg(msg):
                self.janus_ws_backoff.reset()

        self.janus_ws = WebSocketClient(
            'ws://{}:{}/'.format(JANUS_SERVER, JANUS_WS_PORT),
            on_ws_msg=on_message,
            on_ws_close=on_close,
            subprotocols=['janus-protocol'],
            waitsecs=5)

    def shutdown(self):
        self.shutting_down = True

        if self.janus_ws is not None:
            self.janus_ws.close()

        self.janus_ws = None

        if self.janus_proc:
            try:
                self.janus_proc.terminate()
            except Exception:
                pass

        self.janus_proc = None

    def process_janus_msg(self, raw_msg):
        try:
            msg = json.loads(raw_msg)

            # when plugindata.data.obico is set, this is a incoming message from webrtc data channel
            # https://github.com/TheSpaghettiDetective/janus-gateway/commit/e0bcc6b40f145ce72e487204354486b2977393ea
            to_plugin = msg.get('plugindata', {}).get('data', {}).get('thespaghettidetective', {})

            if to_plugin:
                _logger.debug('Processing WebRTC data channel msg from client:')
                _logger.debug(msg)
                # TODO: make data channel work again
                # self.plugin.client_conn.on_message_to_plugin(to_plugin)
                return

            _logger.debug('Relaying Janus msg')
            _logger.debug(msg)
            self.server_conn.send_ws_msg_to_server(dict(janus=raw_msg))
        except:
            self.sentry.captureException()
