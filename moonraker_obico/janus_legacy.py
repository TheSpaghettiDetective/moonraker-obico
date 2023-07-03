import os
import logging
import subprocess
import time
from threading import Thread
import backoff
import json
import socket
import psutil

from .utils import ExpoBackoff, pi_version, to_unicode, is_port_open
from .ws import WebSocketClient
from .webcam_stream import WebcamStreamer

_logger = logging.getLogger('obico.janus')

JANUS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'janus')
JANUS_SERVER = os.getenv('JANUS_SERVER', '127.0.0.1')
JANUS_WS_PORT = 17058
JANUS_PRINTER_DATA_PORT = 17739
MAX_PAYLOAD_SIZE = 1500  # hardcoded in streaming plugin
CAMERA_STREAMER_RTSP_PORT = 8554


class JanusNotSupportedException(Exception):
    pass


class JanusConn:

    def __init__(self, app_model, server_conn, sentry):
        self.config = app_model.config
        self.app_model = app_model
        self.server_conn = server_conn
        self.sentry = sentry
        self.janus_ws_backoff = ExpoBackoff(120, max_attempts=20)
        self.janus_ws = None
        self.janus_proc = None
        self.shutting_down = False
        self.webcam_streamer = None
        self.use_camera_streamer_rtsp = False

    def start(self):

        if os.getenv('JANUS_SERVER', '').strip() != '':
            _logger.warning('Using an external Janus gateway. Not starting the built-in Janus gateway.')
            self.start_janus_ws()
            return

        def run_janus_forever():

            def setup_janus_config():
                video_enabled = 'false' if self.config.webcam.disable_video_streaming else 'true'
                auth_token = self.config.server.auth_token

                cmd_path = os.path.join(JANUS_DIR, 'setup.sh')
                setup_cmd = '{} -A {} -V {}'.format(cmd_path, auth_token, video_enabled)
                if self.use_camera_streamer_rtsp:
                    setup_cmd += ' -r'

                _logger.debug('Popen: {}'.format(setup_cmd))
                setup_proc = psutil.Popen(setup_cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

                returncode = setup_proc.wait()
                (stdoutdata, stderrdata) = setup_proc.communicate()
                if returncode != 0:
                    raise JanusNotSupportedException('Janus setup failed. Skipping Janus connection. Error: \n{}'.format(stdoutdata))

            @backoff.on_exception(backoff.expo, Exception, max_tries=5)
            def run_janus():
                janus_cmd = os.path.join(JANUS_DIR, 'run.sh')
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
                setup_janus_config()
                run_janus()
            except JanusNotSupportedException as e:
                _logger.warning(e)
            except Exception as ex:
                self.sentry.captureException()
                self.server_conn.post_printer_event_to_server(
                    'moonraker-obico: Webcam Streaming Failed',
                    'The webcam streaming failed to start. Obico is now streaming at 0.1 FPS.',
                    event_class='WARNING',
                    info_url='https://www.obico.io/docs/user-guides/webcam-stream-stuck-at-1-10-fps/',
                )

        self.use_camera_streamer_rtsp = self.app_model.linked_printer.get('is_pro') and is_port_open('127.0.0.1', CAMERA_STREAMER_RTSP_PORT)
        _logger.debug(f'Using camera streamer RSTP? {self.use_camera_streamer_rtsp}')

        self.webcam_streamer = WebcamStreamer(self.app_model, self.server_conn, self.sentry)
        if not self.config.webcam.disable_video_streaming and not self.use_camera_streamer_rtsp:
            _logger.info('Starting webcam streamer')
            stream_thread = Thread(target=self.webcam_streamer.video_pipeline)
            stream_thread.daemon = True
            stream_thread.start()

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

        if self.webcam_streamer:
            self.webcam_streamer.restore()

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
