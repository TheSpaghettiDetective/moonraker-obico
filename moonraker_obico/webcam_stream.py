import io
import re
import os
import logging
import subprocess
import time
import sys
import socket
import base64
from collections import deque
from threading import Thread
import backoff
from urllib.error import URLError, HTTPError
import requests

from .utils import get_image_info, pi_version, to_unicode, ExpoBackoff
from .webcam_capture import capture_jpeg

_logger = logging.getLogger('obico.webcam_stream')

JANUS_SERVER = os.getenv('JANUS_SERVER', '127.0.0.1')
JANUS_MJPEG_DATA_PORT = 17740
FFMPEG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'ffmpeg')
FFMPEG = os.path.join(FFMPEG_DIR, 'run.sh')

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


class WebcamStreamer:

    def __init__(self, app_model, server_conn, sentry, janus):
        self.config = app_model.config
        self.app_model = app_model
        self.server_conn = server_conn
        self.sentry = sentry
        self.janus = janus

        self.ffmpeg_proc = None
        self.mjpeg_sock = None
        self.shutting_down = False


    @backoff.on_exception(backoff.expo, Exception)
    def mjpeg_loop(self):
        min_interval_btw_frames = 1.0 / self.config.webcam.get_target_fps(fallback_fps=3)
        bandwidth_throttle = 0.004

        self.mjpeg_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        last_frame_sent = 0

        while True:
            if self.shutting_down:
                return

            if not self.janus.connected():
                time.sleep(1)
                continue

            time.sleep( max(last_frame_sent + min_interval_btw_frames - time.time(), 0) )
            last_frame_sent = time.time()

            jpg = None
            try:
                jpg = capture_jpeg(self.config.webcam)
            except Exception as e:
                _logger.warning('Failed to capture jpeg - ' + str(e))

            if not jpg:
                continue

            encoded = base64.b64encode(jpg)
            self.mjpeg_sock.sendto(bytes('\r\n{}:{}\r\n'.format(len(encoded), len(jpg)), 'utf-8'), (JANUS_SERVER, JANUS_MJPEG_DATA_PORT)) # simple header format for client to recognize
            for chunk in [encoded[i:i+1400] for i in range(0, len(encoded), 1400)]:
                self.mjpeg_sock.sendto(chunk, (JANUS_SERVER, JANUS_MJPEG_DATA_PORT))
                time.sleep(bandwidth_throttle)

    def video_pipeline(self):
        if not pi_version():
            _logger.info('Not on a Raspberry Pi. Switching to MJPEG streaming')
            self.mjpeg_loop()
            return

        try:
            self.ffmpeg_from_mjpeg()

        except Exception:
            self.sentry.captureException()
            self.server_conn.post_printer_event_to_server(
                'moonraker-obico: Webcam Streaming Failed',
                'The webcam streaming failed to start. Obico is now streaming at 0.1 FPS.',
                event_class='WARNING',
                info_url='https://www.obico.io/docs/user-guides/webcam-stream-stuck-at-1-10-fps/',
            )


    def ffmpeg_from_mjpeg(self):

        @backoff.on_exception(backoff.expo, Exception, max_tries=20)  # Retry 20 times in case the webcam service starts later than Obico service
        def get_webcam_resolution(webcam_config):
            return get_image_info(capture_jpeg(webcam_config, force_stream_url=True))

        def h264_encoder():
            test_video = os.path.join(FFMPEG_DIR, 'test-video.mp4')
            FNULL = open(os.devnull, 'w')
            for encoder in ['h264_omx', 'h264_v4l2m2m']:
                ffmpeg_cmd = '{} -re -i {} -pix_fmt yuv420p -vcodec {} -an -f rtp rtp://127.0.0.1:8014?pkt_size=1300'.format(FFMPEG, test_video, encoder)
                _logger.debug('Popen: {}'.format(ffmpeg_cmd))
                ffmpeg_test_proc = subprocess.Popen(ffmpeg_cmd.split(' '), stdout=FNULL, stderr=FNULL)
                if ffmpeg_test_proc.wait() == 0:
                    if encoder == 'h264_omx':
                        return '-flags:v +global_header -c:v {} -bsf dump_extra'.format(encoder)  # Apparently OMX encoder needs extra param to get the stream to work
                    else:
                        return '-c:v {}'.format(encoder)

            raise Exception('No ffmpeg found, or ffmpeg does NOT support h264_omx/h264_v4l2m2m encoding.')

        if self.app_model.linked_printer.get('is_pro'):
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

        webcam_config = self.config.webcam
        stream_url = webcam_config.stream_url
        if not stream_url:
            raise Exception('stream_url not configured. Unable to stream the webcam.')

        # crowsnest starts with a "NO SIGNAL" stream that is always 640x480. Wait for a few seconds to make sure it has the time to start a real stream
        time.sleep(15)
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
        fps = webcam_config.get_target_fps(fallback_fps=25)
        if not self.app_model.linked_printer.get('is_pro'):
            fps = min(8, fps) # For some reason, when fps is set to 5, it looks like 2FPS. 8fps looks more like 5
            bitrate = int(bitrate/2)

        self.start_ffmpeg('-re -i {} -filter:v fps={} -b:v {} -pix_fmt yuv420p -s {}x{} {}'.format(stream_url, fps, bitrate, img_w, img_h, encoder))

    def start_ffmpeg(self, ffmpeg_args, retry_after_quit=False):
        ffmpeg_cmd = '{} -loglevel error {} -an -f rtp rtp://{}:17734?pkt_size=1300'.format(FFMPEG, ffmpeg_args, JANUS_SERVER)

        _logger.debug('Popen: {}'.format(ffmpeg_cmd))
        FNULL = open(os.devnull, 'w')
        self.ffmpeg_proc = subprocess.Popen(ffmpeg_cmd.split(' '), stdin=subprocess.PIPE, stdout=FNULL, stderr=subprocess.PIPE)

        try:
            returncode = self.ffmpeg_proc.wait(timeout=10) # If ffmpeg fails, it usually does so without 10s
            (stdoutdata, stderrdata) = self.ffmpeg_proc.communicate()
            msg = 'STDOUT:\n{}\nSTDERR:\n{}\n'.format(stdoutdata, stderrdata)
            _logger.error(msg)
            raise Exception('ffmpeg failed! Exit code: {}'.format(returncode))
        except subprocess.TimeoutExpired:
           pass

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

                    if retry_after_quit:
                        ffmpeg_backoff.more('ffmpeg exited un-expectedly. Exit code: {}'.format(returncode))
                        ring_buffer = deque(maxlen=50)
                        _logger.debug('Popen: {}'.format(ffmpeg_cmd))
                        self.ffmpeg_proc = subprocess.Popen(ffmpeg_cmd.split(' '), stdin=subprocess.PIPE, stdout=FNULL, stderr=subprocess.PIPE)
                    else:
                        self.sentry.captureMessage('ffmpeg exited un-expectedly. Exit code: {}'.format(returncode))
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

        if self.mjpeg_sock:
            self.mjpeg_sock.close()

        self.ffmpeg_proc = None
        self.mjpeg_sock = None
