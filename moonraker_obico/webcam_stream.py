import io
import re
import os
import logging
import subprocess
import time
import sys
from collections import deque
from threading import Thread
import backoff
from urllib.error import URLError, HTTPError
import requests
import base64
import socket
import traceback

from .utils import get_image_info, pi_version, to_unicode, ExpoBackoff, parse_integer_or_none
from .webcam_capture import capture_jpeg
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


class JanusNotFoundException(Exception):
  pass


@backoff.on_exception(backoff.expo, Exception, max_tries=3)
def get_webcam_resolution(webcam_config):
    (img_w, img_h) = (640, 360)
    try:
        (_, img_w, img_h) = get_image_info(capture_jpeg(webcam_config, force_stream_url=True))
        _logger.debug(f'Detected webcam resolution - w:{img_w} / h:{img_h}')
    except Exception:
        _logger.exception('Failed to connect to webcam to retrieve resolution. Using default.')

    return (img_w, img_h)


def find_ffmpeg_h264_encoder():
    test_video = os.path.join(FFMPEG_DIR, 'test-video.mp4')
    FNULL = open(os.devnull, 'w')
    try:
        for encoder in ['h264_omx', 'h264_v4l2m2m']:
            ffmpeg_cmd = '{} -re -i {} -pix_fmt yuv420p -vcodec {} -an -f rtp rtp://127.0.0.1:8014?pkt_size=1300'.format(FFMPEG, test_video, encoder)
            _logger.debug('Popen: {}'.format(ffmpeg_cmd))
            ffmpeg_test_proc = subprocess.Popen(ffmpeg_cmd.split(' '), stdout=FNULL, stderr=FNULL)
            if ffmpeg_test_proc.wait() == 0:
                if encoder == 'h264_omx':
                    return '-flags:v +global_header -c:v {} -bsf dump_extra'.format(encoder)  # Apparently OMX encoder needs extra param to get the stream to work
                else:
                    return '-c:v {}'.format(encoder)
    except Exception as e:
        _logger.exception('Failed to find ffmpeg h264 encoder. Exception: %s\n%s', e, traceback.format_exc())

    _logger.warn('No ffmpeg found, or ffmpeg does NOT support h264_omx/h264_v4l2m2m encoding.')
    return None

class DataChannelOnlyWebcamConfig: # Stub WebcamConfig for building janus config
    pass


class WebcamStreamer:

    def __init__(self, server_conn, moonrakerconn, client_conn, app_model, sentry):
        self.server_conn = server_conn
        self.moonrakerconn = moonrakerconn
        self.client_conn = client_conn
        self.printer_state = app_model.printer_state
        self.app_config = app_model.config
        self.is_pro = app_model.linked_printer.get('is_pro')
        self.sentry = sentry
        self.ffmpeg_out_rtp_ports = set()
        self.mjpeg_sock_list = []
        self.janus = None
        self.ffmpeg_proc = None
        self.shutting_down = False

    ## The methods that are available as passthru target

    def start(self, webcam_configs):

        self.shutdown_subprocesses()
        self.close_all_mjpeg_socks()

        self.webcams = webcam_configs
        self.find_streaming_params()
        self.assign_janus_params()
        normalized_webcams = []
        streaming_error = None

        try:
            webcams_to_build_janus_config = [webcam for webcam in self.webcams]
            data_channel = next((webcam for webcam in webcams_to_build_janus_config if webcam.runtime.get('dataport')), None)
            if not data_channel:
                data_channel = DataChannelOnlyWebcamConfig()
                data_channel.streaming_params = dict(mode='data_channel_only')
                data_channel.runtime = dict(stream_id=389, dataport=JANUS_WS_PORT+389) # A random stream_id and port that is unlikely to conflict
                webcams_to_build_janus_config.append(data_channel)

            (janus_bin_path, ld_lib_path) = build_janus_config(webcams_to_build_janus_config, self.app_config.server.auth_token, JANUS_WS_PORT, JANUS_ADMIN_WS_PORT)
            if not janus_bin_path:
                raise JanusNotFoundException('Janus not found or not configured correctly. Click the link for troubleshooting guide.')

            self.janus = JanusConn(JANUS_WS_PORT, self.app_config, self.server_conn, self.is_pro, self.sentry)
            self.janus.start(janus_bin_path, ld_lib_path)

            if not self.wait_for_janus():
                for webcam in self.webcams:
                    webcam.setdefault('error', 'Janus failed to start')

            for webcam in self.webcams:
                if webcam.streaming_params['mode'] == 'h264_rtsp':
                    continue    # No extra process is needed when the mode is 'h264_rtsp'
                elif webcam.streaming_params['mode'] == 'h264_copy':
                    self.h264_copy(webcam)
                elif webcam.streaming_params['mode'] == 'h264_device':
                    self.h264_device(webcam)
                elif webcam.streaming_params['mode'] == 'h264_transcode':
                    self.h264_transcode(webcam)
                elif webcam.streaming_params['mode'] == 'mjpeg_webrtc':
                    self.mjpeg_webrtc(webcam)

            normalized_webcams = [self.normalized_webcam_dict(webcam) for webcam in self.webcams]

            self.client_conn.open_data_channel(data_channel.runtime['dataport'])

        except JanusNotFoundException as e:
            streaming_error = str(e)
            _logger.error(f'{e} Webcam is now streaming in 0.1FPS.', exc_info=True)
            self.send_streaming_failed_event(streaming_error, info_url='https://obico.io/docs/user-guides/webcam-install-janus/')
            self.shutdown()
            # When Janus is not found, we will stream the primary camera in 0.1FPS. This provides a better user experience, and is compatible with old mobile app versions
            normalized_webcams = [self.normalized_webcam_dict(webcam) for webcam in self.webcams if webcam.is_primary_camera]

        except Exception as e:
            streaming_error = str(e)
            self.sentry.captureException()
            self.send_streaming_failed_event()
            self.shutdown()

        finally:
            self.printer_state.set_webcams(normalized_webcams, data_channel.runtime.get('stream_id') if data_channel else None)
            self.server_conn.post_status_update_to_server(with_settings=True)
            return (normalized_webcams, streaming_error)  # return value expected for a passthru target

    def shutdown(self):
        self.shutting_down = True
        self.shutdown_subprocesses()
        self.close_all_mjpeg_socks()
        return ('ok', None)  # return value expected for a passthru target

    def send_streaming_failed_event(self, error=None, info_url='https://obico.io/docs/user-guides/moonraker-obico/webcam/'):
        self.server_conn.post_printer_event_to_server(
            'moonraker-obico: Webcam Streaming Failed',
            error if error else f'Make sure the webcam is properly configured in moonraker-obico.cfg.',
            event_class='WARNING',
            info_url=info_url
        )

    def find_streaming_params(self):
        ffmpeg_h264_encoder = find_ffmpeg_h264_encoder()
        webcams = []
        for webcam in self.webcams:
            stream_mode = webcam.stream_mode if webcam.stream_mode else ('h264_transcode' if ffmpeg_h264_encoder else 'mjpeg_webrtc')
            webcam.streaming_params = dict(
                    mode=stream_mode,
                    h264_encoder=ffmpeg_h264_encoder,
            )

            try:
                (img_w, img_h) = map(int, webcam.resolution.split('x'))
                webcam.streaming_params['recode_width'] = img_w
                webcam.streaming_params['recode_height'] = img_h
            except:
                _logger.warn('Resolution not specified or invalid in webcam config. Getting the values from the source.')

            try:
                webcam.streaming_params['recode_fps'] = webcam.target_fps
            except:
                _logger.warn('FPS not specified or invalid in webcam config. Getting the values from the source.')

    def assign_janus_params(self):
        first_h264_webcam = next(filter(lambda item: 'h264' in item.streaming_params['mode'] and item.is_primary_camera, self.webcams), None)
        if first_h264_webcam:
            first_h264_webcam.runtime = {}
            first_h264_webcam.runtime['stream_id'] = 1  # Set janus id to 1 for the first h264 stream to be compatible with old mobile app versions

        first_mjpeg_webcam = next(filter(lambda item: 'mjpeg' in item.streaming_params['mode'] and item.is_primary_camera, self.webcams), None)
        if first_mjpeg_webcam:
            first_mjpeg_webcam.runtime = {}
            first_mjpeg_webcam.runtime['stream_id'] = 2  # Set janus id to 2 for the first mjpeg stream to be compatible with old mobile app versions

        cur_stream_id = 3
        cur_port_num = JANUS_ADMIN_WS_PORT + 1
        for webcam in self.webcams:
            if not hasattr(webcam, 'runtime'):
                webcam.runtime = {}

            if not webcam.runtime.get('stream_id'):
                webcam.runtime['stream_id'] = cur_stream_id
                cur_stream_id += 1

            if webcam.streaming_params['mode'] in ('h264_copy', 'h264_transcode', 'h264_device'):
                 webcam.runtime['videoport'] = cur_port_num
                 cur_port_num += 1
                 webcam.runtime['videortcpport'] = cur_port_num
                 cur_port_num += 1
                 webcam.runtime['dataport'] = cur_port_num
                 cur_port_num += 1
            elif webcam.streaming_params['mode'] == 'mjpeg_webrtc':
                 webcam.runtime['mjpeg_dataport'] = cur_port_num
                 cur_port_num += 1


    def wait_for_janus(self):
        for i in range(100):
            time.sleep(0.1)
            if self.janus and self.janus.janus_ws and self.janus.janus_ws.connected():
                return True

        return False


    def h264_copy(self, webcam):
        try:
            if not self.is_pro:
                raise Exception('Free user can not stream webcam in h264_copy mode')

            # There seems to be a bug in camera-streamer that causes to close .mp4 connection after a random period of time. In that case, we rerun ffmpeg
            self.start_ffmpeg(webcam.runtime['videoport'], f'-re -i {webcam.h264_http_url} -c:v copy', retry_after_quit=True)
        except Exception:
            self.sentry.captureException()


    def h264_device(self, webcam):
        try:
            (img_w, img_h) = (parse_integer_or_none(webcam.streaming_params.get('recode_width')), parse_integer_or_none(webcam.streaming_params.get('recode_height')))
            if not img_w or not img_h:
                _logger.warn('width and/or height not specified or invalid for h264_device mode. Using default.')
                (img_w, img_h) = (640, 480)

            rtp_port = webcam.runtime['videoport']

            self.start_ffmpeg(rtp_port, f'-f v4l2 -framerate {webcam.target_fps} -s {img_w}x{img_h} -input_format h264 -i {webcam.h264_device_path} -c:v copy -flags:v +global_header')
        except Exception:
            self.sentry.captureException()


    def h264_transcode(self, webcam):

        try:
            stream_url = webcam.stream_url
            if not stream_url:
                raise Exception('stream_url not configured. Unable to stream the webcam.')

            (img_w, img_h) = (parse_integer_or_none(webcam.streaming_params.get('recode_width')), parse_integer_or_none(webcam.streaming_params.get('recode_height')))
            if not img_w or not img_h:
                _logger.warn('width and/or height not specified or invalid in streaming parameters. Getting the values from the source.')
                (img_w, img_h) = get_webcam_resolution(webcam)

            fps = parse_integer_or_none(webcam.streaming_params.get('recode_fps'))
            if not fps:
                _logger.warn('FPS not specified or invalid in streaming parameters. Getting the values from the source.')
                fps = webcam.target_fps

            bitrate = bitrate_for_dim(img_w, img_h)
            if not self.is_pro:
                fps = min(8, fps) # For some reason, when fps is set to 5, it looks like 2FPS. 8fps looks more like 5
                bitrate = int(bitrate/2)

            rtp_port = webcam.runtime['videoport']
            self.start_ffmpeg(rtp_port, '-re -i {stream_url} -filter:v fps={fps} -b:v {bitrate} -pix_fmt yuv420p -s {img_w}x{img_h} {encoder}'.format(stream_url=stream_url, fps=fps, bitrate=bitrate, img_w=img_w, img_h=img_h, encoder=webcam.streaming_params.get('h264_encoder')))
        except Exception:
            self.sentry.captureException()


    @backoff.on_exception(backoff.expo, Exception, base=3, jitter=None, max_tries=5) # Apparently crowsnest would return invalid mjpeg data right after start and hence throw off ffmpeg. We should retry in this case
    def start_ffmpeg(self, rtp_port, ffmpeg_args, retry_after_quit=False):
        ffmpeg_cmd = '{ffmpeg} -loglevel error {ffmpeg_args} -an -f rtp rtp://{janus_server}:{rtp_port}?pkt_size=1300'.format(ffmpeg=FFMPEG, ffmpeg_args=ffmpeg_args, janus_server=JANUS_SERVER, rtp_port=rtp_port)

        _logger.debug('Popen: {}'.format(ffmpeg_cmd))
        FNULL = open(os.devnull, 'w')
        ffmpeg_proc = subprocess.Popen(ffmpeg_cmd.split(' '), stdin=subprocess.PIPE, stdout=FNULL, stderr=subprocess.PIPE)

        self.ffmpeg_out_rtp_ports.add(str(rtp_port))

        with open(self.ffmpeg_pid_file_path(rtp_port), 'w') as pid_file:
            pid_file.write(str(ffmpeg_proc.pid))

        try:
            returncode = ffmpeg_proc.wait(timeout=10) # If ffmpeg fails, it usually does so without 10s
            (stdoutdata, stderrdata) = ffmpeg_proc.communicate()
            msg = 'STDOUT:\n{}\nSTDERR:\n{}\n'.format(stdoutdata, stderrdata)
            _logger.error(msg)
            raise Exception('ffmpeg failed! Exit code: {}'.format(returncode))
        except subprocess.TimeoutExpired:
           pass

        def monitor_ffmpeg_process(ffmpeg_proc, retry_after_quit=False):
            # It seems important to drain the stderr output of ffmpeg, otherwise the whole process will get clogged
            ring_buffer = deque(maxlen=50)
            ffmpeg_backoff = ExpoBackoff(3)
            while True:
                line = to_unicode(ffmpeg_proc.stderr.readline(), errors='replace')
                if not line:  # line == None means the process quits
                    if self.shutting_down:
                        return

                    returncode = ffmpeg_proc.wait()
                    msg = 'STDERR:\n{}\n'.format('\n'.join(ring_buffer))
                    _logger.debug(msg)

                    if retry_after_quit:
                        ffmpeg_backoff.more('ffmpeg exited un-expectedly. Exit code: {}'.format(returncode))
                        ring_buffer = deque(maxlen=50)
                        _logger.debug('Popen: {}'.format(ffmpeg_cmd))
                        ffmpeg_proc = subprocess.Popen(ffmpeg_cmd.split(' '), stdin=subprocess.PIPE, stdout=FNULL, stderr=subprocess.PIPE)
                    else:
                        self.sentry.captureMessage('ffmpeg exited un-expectedly. Exit code: {}'.format(returncode))
                        return
                else:
                    ring_buffer.append(line)

        ffmpeg_thread = Thread(target=monitor_ffmpeg_process, kwargs=dict(ffmpeg_proc=ffmpeg_proc, retry_after_quit=retry_after_quit))
        ffmpeg_thread.daemon = True
        ffmpeg_thread.start()

    def mjpeg_webrtc(self, webcam):

        @backoff.on_exception(backoff.expo, Exception)
        def mjpeg_loop():

            mjpeg_dataport = webcam.runtime['mjpeg_dataport']

            min_interval_btw_frames = 1.0 / webcam.target_fps
            bandwidth_throttle = 0.004
            if pi_version() == "0":    # If Pi Zero
                bandwidth_throttle *= 2

            mjpeg_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.mjpeg_sock_list.append(mjpeg_sock)

            last_frame_sent = 0

            while True:
                if self.shutting_down:
                    return

                time.sleep( max(last_frame_sent + min_interval_btw_frames - time.time(), 0) )
                last_frame_sent = time.time()

                jpg = None
                try:
                    jpg = capture_jpeg(webcam)
                except Exception as e:
                    _logger.warning('Failed to capture jpeg - ' + str(e))

                if not jpg:
                    continue

                encoded = base64.b64encode(jpg)
                mjpeg_sock.sendto(bytes('\r\n{}:{}\r\n'.format(len(encoded), len(jpg)), 'utf-8'), (JANUS_SERVER, mjpeg_dataport)) # simple header format for client to recognize
                for chunk in [encoded[i:i+1400] for i in range(0, len(encoded), 1400)]:
                    mjpeg_sock.sendto(chunk, (JANUS_SERVER, mjpeg_dataport))
                    time.sleep(bandwidth_throttle)

        mjpeg_loop_thread = Thread(target=mjpeg_loop)
        mjpeg_loop_thread.daemon = True
        mjpeg_loop_thread.start()

    def ffmpeg_pid_file_path(self, rtp_port):
        return '/tmp/obico-ffmpeg-{rtp_port}.pid'.format(rtp_port=rtp_port)

    def kill_all_ffmpeg_if_running(self):
        for rtc_port in self.ffmpeg_out_rtp_ports:
            self.kill_ffmpeg_if_running(rtc_port)

        self.ffmpeg_out_rtp_ports = set()

    def kill_ffmpeg_if_running(self, rtc_port):
        # It is possible that some orphaned ffmpeg process is running (maybe previous python process was killed -9?).
        # Ensure all ffmpeg processes are killed
        with open(self.ffmpeg_pid_file_path(rtc_port), 'r') as pid_file:
            try:
                subprocess.run(['kill', pid_file.read()], check=True)
            except Exception as e:
                _logger.warning('Failed to shutdown ffmpeg - ' + str(e))

    def shutdown_subprocesses(self):
        if self.janus:
            self.janus.shutdown()
        self.kill_all_ffmpeg_if_running()

    def close_all_mjpeg_socks(self):
        for mjpeg_sock in self.mjpeg_sock_list:
            mjpeg_sock.close()

    def normalized_webcam_dict(self, webcam):
        return dict(
                name=webcam.name,
                is_primary_camera=webcam.is_primary_camera,
                is_nozzle_camera=webcam.is_nozzle_camera,
                stream_mode=webcam.streaming_params.get('mode'),
                stream_id=webcam.runtime.get('stream_id'),
                data_channel_available=webcam.runtime.get('dataport', -1) > 0,
                flipV=webcam.flip_v,
                flipH=webcam.flip_h,
                rotation=webcam.rotation,
                streamRatio='16:9' if webcam.aspect_ratio_169 else '4:3',
                )
