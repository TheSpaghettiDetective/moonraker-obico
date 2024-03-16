import os
import logging
import subprocess
import time
from threading import Thread
import backoff
import json
import socket

from .utils import pi_version, to_unicode, is_port_open, run_in_thread
from .ws import WebSocketClient
from .janus_config_builder import RUNTIME_JANUS_ETC_DIR
#from .webcam_stream import WebcamStreamer

_logger = logging.getLogger('obico.janus')

JANUS_SERVER = os.getenv('JANUS_SERVER', '127.0.0.1')
# CAMERA_STREAMER_RTSP_PORT = 8554


class JanusConn:

    def __init__(self, janus_port, app_config, server_conn, is_pro, sentry):
        self.janus_port = janus_port
        self.app_config = app_config
        self.server_conn = server_conn
        self.is_pro = is_pro
        self.sentry = sentry
        self.janus_ws = None
        self.shutting_down = False
        #self.webcam_streamer = None
        self.use_camera_streamer_rtsp = False

    def start(self, janus_bin_path, ld_lib_path):

        if os.getenv('JANUS_SERVER', '').strip() != '':
            _logger.warning('Using an external Janus gateway. Not starting the built-in Janus gateway.')
            self.start_janus_ws()
            return

        def run_janus_forever():
            try:
                self.kill_janus_if_running()

                janus_cmd = '{janus_bin_path} --stun-server=stun.l.google.com:19302 --configs-folder {config_folder}'.format(janus_bin_path=janus_bin_path, config_folder=RUNTIME_JANUS_ETC_DIR)
                env = {}
                if ld_lib_path:
                    env={'LD_LIBRARY_PATH': ld_lib_path + ':' + os.environ.get('LD_LIBRARY_PATH', '')}
                _logger.debug('Popen: {} {}'.format(env, janus_cmd))
                janus_proc = subprocess.Popen(janus_cmd.split(), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

                with open(self.janus_pid_file_path(), 'w') as pid_file:
                    pid_file.write(str(janus_proc.pid))

                while True:
                    line = to_unicode(janus_proc.stdout.readline(), errors='replace')
                    if line:
                        _logger.debug('JANUS: ' + line.rstrip())
                    else:  # line == None means the process quits
                        _logger.warn('Janus quit with exit code {}'.format(janus_proc.wait()))
                        return
            except Exception as ex:
                self.sentry.captureException()

        #self.use_camera_streamer_rtsp = self.is_pro and is_port_open('127.0.0.1', CAMERA_STREAMER_RTSP_PORT)
        #_logger.debug(f'Using camera streamer RSTP? {self.use_camera_streamer_rtsp}')

#        self.webcam_streamer = WebcamStreamer(self.app_model, self.server_conn, self.sentry)
#        if not self.config.webcam.disable_video_streaming and not self.use_camera_streamer_rtsp:
#            _logger.info('Starting webcam streamer')
#            stream_thread = Thread(target=self.webcam_streamer.video_pipeline)
#            stream_thread.daemon = True
#            stream_thread.start()

        run_in_thread(run_janus_forever)

        self.wait_for_janus()
        self.start_janus_ws()

    def connected(self):
        return self.janus_ws and self.janus_ws.connected()

    def pass_to_janus(self, msg):
        if self.connected():
            self.janus_ws.send(msg)

    @backoff.on_exception(backoff.expo, Exception, max_tries=10)
    def wait_for_janus(self):
        time.sleep(0.2)
        socket.socket().connect((JANUS_SERVER, self.janus_port))

    def start_janus_ws(self):

        def on_close(ws, **kwargs):
            _logger.warn('Janus WS connection closed!')

        self.janus_ws = WebSocketClient(
            'ws://{}:{}/'.format(JANUS_SERVER, self.janus_port),
            on_ws_msg=self.process_janus_msg,
            on_ws_close=on_close,
            subprotocols=['janus-protocol'],
            waitsecs=5)

    def janus_pid_file_path(self):
        return '/tmp/obico-janus-{janus_port}.pid'.format(janus_port=self.janus_port)

    def kill_janus_if_running(self):
        try:
            # It is possible that orphaned janus process is running (maybe previous python process was killed -9?).
            # Ensure the process is killed before launching a new one
            with open(self.janus_pid_file_path(), 'r') as pid_file:
                subprocess.run(['kill', '-9', pid_file.read()], check=True)
        except Exception as e:
            _logger.warning('Failed to shutdown Janus - ' + str(e))

    def shutdown(self):
        self.shutting_down = True

        if self.janus_ws is not None:
            self.janus_ws.close()

        self.janus_ws = None

        self.kill_janus_if_running()

#        if self.webcam_streamer:
#            self.webcam_streamer.restore()
#
    def process_janus_msg(self, ws, raw_msg):
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
