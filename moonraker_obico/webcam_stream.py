import io
import re
import os
import logging
import subprocess
import time
import sys
from collections import deque
from threading import Thread
import psutil
import backoff
from urllib.error import URLError, HTTPError
import requests

from .utils import get_image_info, pi_version, to_unicode, ExpoBackoff
from .webcam_capture import capture_jpeg, webcam_full_url
from .janus import JanusConn
from .janus_config_builder import build_janus_config

_logger = logging.getLogger('obico.webcam_stream')

JANUS_SERVER = os.getenv('JANUS_SERVER', '127.0.0.1')
FFMPEG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'ffmpeg')
FFMPEG = os.path.join(FFMPEG_DIR, 'run.sh')

JANUS_WS_PORT = 17730   # Janus needs to use 17730 up to 17750. Hard-coded for now. may need to make it dynamic if the problem of port conflict is too much
JANUS_ADMIN_WS_PORT = JANUS_WS_PORT + 1

PI_CAM_RESOLUTIONS = {
    'low': ((320, 240), (480, 270)),  # resolution for 4:3 and 16:9
    'medium': ((640, 480), (960, 540)),
    'high': ((1296, 972), (1640, 922)),
    'ultra_high': ((1640, 1232), (1920, 1080)),
}

def bitrate_for_dim(img_w, img_h):
    dim = img_w * img_h
    if dim <= 480 * 270:
        return 400*1000
    if dim <= 960 * 540:
        return 1300*1000
    if dim <= 1280 * 720:
        return 2000*1000
    else:
        return 3000*1000

def cpu_watch_dog(watched_process, max, interval, server_conn):

    def watch_process_cpu(watched_process, max, interval, server_conn):
        while True:
            if not watched_process.is_running():
                return

            cpu_pct = watched_process.cpu_percent(interval=None)
            if cpu_pct > max:
                server_conn.post_printer_event_to_server(
                    'moonraker-obico: Webcam Streaming Using Excessive CPU',
                    'The webcam streaming uses excessive CPU. This may negatively impact your print quality, or cause webcam streaming issues.',
                    event_class='WARNING',
                    info_url='https://obico.io/docs/user-guides/webcam-streaming-resolution-framerate-klipper/',
                )

            time.sleep(interval)

    watch_thread = Thread(target=watch_process_cpu, args=(watched_process, max, interval, server_conn))
    watch_thread.daemon = True
    watch_thread.start()


class WebcamStreamer:

    def __init__(self, server_conn, moonrakerconn, app_config, linked_printer, sentry):
        self.server_conn = server_conn
        self.moonrakerconn = moonrakerconn
        self.app_config = app_config
        self.linked_printer = linked_printer
        self.sentry = sentry

        self.janus = None

    def run_pipeline(self):
        moonraker_webcams = (self.moonrakerconn.api_get('server.webcams.list', raise_for_status=False) or {}).get('webcams', [])
        self.webcams = []
        for webcam in self.linked_printer.get('cameras', []):
            moonraker_webcam = next(filter(lambda item: item.get('name') == webcam['name'], moonraker_webcams), None) # Find a Moonraker webcam that matches the name, or None if not found
            if moonraker_webcam is None or not moonraker_webcam.get('enabled'):
                webcam['error'] = '{webcam_name} is not configured in Moonraker, or is disabled.'.format(webcam_name=webcam['name'])
            else:
                webcam['moonraker_config'] = moonraker_webcam

            self.webcams.append(webcam)

        # TODO: construct self.webcams if cameras are not configured in Obico

        self.assign_janus_params()
        (janus_bin_path, ld_lib_path) = build_janus_config(self.webcams, self.app_config.server.auth_token, JANUS_WS_PORT, JANUS_ADMIN_WS_PORT)
        self.janus = JanusConn(self.app_config, self.server_conn, self.linked_printer.get('is_pro'), self.sentry)
        self.janus.start(JANUS_WS_PORT, janus_bin_path, ld_lib_path)


    def assign_janus_params(self):
        # TODO: reorder self.webcams so that, if possible, it's compatible with old mobile app versions

        cur_janus_section_id = 1
        cur_port_num = JANUS_ADMIN_WS_PORT + 1
        for webcam in self.webcams:
            webcam['runtime'] = {}
            webcam['runtime']['janus_section_id'] = cur_janus_section_id
            cur_janus_section_id += 1

            if webcam['config']['mode'] == 'h264-rtsp':
                 webcam['runtime']['dataport'] = cur_port_num
                 cur_port_num += 1
            elif webcam['config']['mode'] in ('h264-copy', 'h264-recode'):
                 webcam['runtime']['videoport'] = cur_port_num
                 cur_port_num += 1
                 webcam['runtime']['videortcpport'] = cur_port_num
                 cur_port_num += 1
                 webcam['runtime']['dataport'] = cur_port_num
                 cur_port_num += 1
            elif webcam['config']['mode'] == 'mjpeg':
                 webcam['runtime']['mjpeg_dataport'] = cur_port_num
                 cur_port_num += 1


    def start(self, webcam_name, **kwargs):

        webcam_config = (self.moonrakerconn.api_get('server/webcams/item', raise_for_status=False, name=webcam_name) or {}).get('webcam')
        if not isinstance(webcam_config, dict) or not webcam_config.get('enabled'):
            raise Exception(f'{webcam_name} is not configured or is disabled')

        if self.janus:
            self.janus.shutdown()

        if 'mjpeg' in webcam_config.get('service').lower():
            self.janus = JanusConn(self.app_config, self.server_conn, self.linked_printer.get('is_pro'), self.sentry)
            janus_thread = Thread(target=self.janus.start)
            janus_thread.daemon = True
            janus_thread.start()

            if not pi_version():
                _logger.warning('Not running on a Pi. Quitting video_pipeline.')
                return (None, 'Not running on a Pi. Quitting video_pipeline.')

            try:
                self.ffmpeg_from_mjpeg(webcam_config)

            except Exception:
                self.sentry.captureException()

        return ('ok', None)

    def ffmpeg_from_mjpeg(self, webcam_config):

        @backoff.on_exception(backoff.expo, Exception, max_tries=20)  # Retry 20 times in case the webcam service starts later than Obico service
        def get_webcam_resolution(webcam_config):
            return get_image_info(capture_jpeg(webcam_config, force_stream_url=True))

        def h264_encoder():
            test_video = os.path.join(FFMPEG_DIR, 'test-video.mp4')
            FNULL = open(os.devnull, 'w')
            for encoder in ['h264_omx', 'h264_v4l2m2m']:
                ffmpeg_cmd = '{} -re -i {} -pix_fmt yuv420p -vcodec {} -an -f rtp rtp://localhost:8014?pkt_size=1300'.format(FFMPEG, test_video, encoder)
                _logger.debug('Popen: {}'.format(ffmpeg_cmd))
                ffmpeg_test_proc = psutil.Popen(ffmpeg_cmd.split(' '), stdout=FNULL, stderr=FNULL)
                if ffmpeg_test_proc.wait() == 0:
                    if encoder == 'h264_omx':
                        return '-flags:v +global_header -c:v {} -bsf dump_extra'.format(encoder)  # Apparently OMX encoder needs extra param to get the stream to work
                    else:
                        return '-c:v {}'.format(encoder)

            raise Exception('No ffmpeg found, or ffmpeg does NOT support h264_omx/h264_v4l2m2m encoding.')

        if self.linked_printer.get('is_pro'):
            # camera-stream is introduced in Crowsnest V4
            try:
                camera_streamer_mp4_url = 'http://127.0.0.1:8080/video.mp4'
                _logger.info('Trying to start ffmpeg using camera-streamer H.264 source')
                # There seems to be a bug in camera-streamer that causes to close .mp4 connection after a random period of time. In that case, we rerun ffmpeg
                self.start_ffmpeg('-re -i {} -c:v copy'.format(camera_streamer_mp4_url), retry_after_quit=True)
                return
            except Exception as e:
                _logger.info(f'No camera-stream H.264 source found. Continue to legacy streaming: {e}')
                pass

        # The streaming mechansim for pre-1.0 OctoPi versions

        encoder = h264_encoder()

        stream_url = webcam_full_url(webcam_config.get('stream_url'))
        if not stream_url:
            raise Exception('stream_url not configured. Unable to stream the webcam.')

        # crowsnest starts with a "NO SIGNAL" stream that is always 640x480. Wait for a few seconds to make sure it has the time to start a real stream
        #time.sleep(15)
        (img_w, img_h) = (640, 480)
        try:
            (_, img_w, img_h) = get_webcam_resolution(webcam_config)
            _logger.debug(f'Detected webcam resolution - w:{img_w} / h:{img_h}')
        except (URLError, HTTPError, requests.exceptions.RequestException):
            _logger.warn(f'Failed to connect to webcam to retrieve resolution. Using default.')
        except Exception:
            self.sentry.captureException()
            _logger.warn(f'Failed to detect webcam resolution due to unexpected error. Using default.')

        bitrate = bitrate_for_dim(img_w, img_h)
        fps = webcam_config.get('target_fps')
        if not self.linked_printer.get('is_pro'):
            fps = min(8, fps) # For some reason, when fps is set to 5, it looks like 2FPS. 8fps looks more like 5
            bitrate = int(bitrate/2)

        self.start_ffmpeg('-re -i {} -filter:v fps={} -b:v {} -pix_fmt yuv420p -s {}x{} {}'.format(stream_url, fps, bitrate, img_w, img_h, encoder))

    def start_ffmpeg(self, ffmpeg_args, retry_after_quit=False):
        ffmpeg_cmd = '{} -loglevel error {} -an -f rtp rtp://{}:17734?pkt_size=1300'.format(FFMPEG, ffmpeg_args, JANUS_SERVER)

        _logger.debug('Popen: {}'.format(ffmpeg_cmd))
        FNULL = open(os.devnull, 'w')
        self.ffmpeg_proc = psutil.Popen(ffmpeg_cmd.split(' '), stdin=subprocess.PIPE, stdout=FNULL, stderr=subprocess.PIPE)
        self.ffmpeg_proc.nice(10)

        try:
            returncode = self.ffmpeg_proc.wait(timeout=10) # If ffmpeg fails, it usually does so without 10s
            (stdoutdata, stderrdata) = self.ffmpeg_proc.communicate()
            msg = 'STDOUT:\n{}\nSTDERR:\n{}\n'.format(stdoutdata, stderrdata)
            _logger.error(msg)
            raise Exception('ffmpeg failed! Exit code: {}'.format(returncode))
        except psutil.TimeoutExpired:
           pass

        cpu_watch_dog(self.ffmpeg_proc, max=80, interval=20, server_conn=self.server_conn)

        def monitor_ffmpeg_process(retry_after_quit=False):
            # It seems important to drain the stderr output of ffmpeg, otherwise the whole process will get clogged
            ring_buffer = deque(maxlen=50)
            ffmpeg_backoff = ExpoBackoff(3)
            while True:
                err = to_unicode(self.ffmpeg_proc.stderr.readline(), errors='replace')
                if not err:  # EOF when process ends?
                    if self.shutting_down:
                        return

                    returncode = self.ffmpeg_proc.wait()
                    msg = 'STDERR:\n{}\n'.format('\n'.join(ring_buffer))
                    _logger.debug(msg)
                    self.sentry.captureMessage('ffmpeg exited un-expectedly. Exit code: {}'.format(returncode))

                    if retry_after_quit:
                        ffmpeg_backoff.more('ffmpeg exited un-expectedly. Exit code: {}'.format(returncode))
                        ring_buffer = deque(maxlen=50)
                        _logger.debug('Popen: {}'.format(ffmpeg_cmd))
                        self.ffmpeg_proc = psutil.Popen(ffmpeg_cmd.split(' '), stdin=subprocess.PIPE, stdout=FNULL, stderr=subprocess.PIPE)
                    else:
                        return
                else:
                    ring_buffer.append(err)

        ffmpeg_thread = Thread(target=monitor_ffmpeg_process, kwargs=dict(retry_after_quit=retry_after_quit))
        ffmpeg_thread.daemon = True
        ffmpeg_thread.start()


    def restore(self):
        self.shutting_down = True

        if self.ffmpeg_proc:
            try:
                self.ffmpeg_proc.terminate()
            except Exception:
                pass

        self.ffmpeg_proc = None
