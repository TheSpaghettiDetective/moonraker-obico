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

from .utils import get_image_info, pi_version, to_unicode, ExpoBackoff, parse_integer_or_none
from .webcam_capture import capture_jpeg, webcam_full_url
from .janus import JanusConn
from .janus_config_builder import build_janus_config

_logger = logging.getLogger('obico.webcam_stream')

JANUS_SERVER = os.getenv('JANUS_SERVER', '127.0.0.1')
JANUS_MJPEG_DATA_PORT = 17740
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


@backoff.on_exception(backoff.expo, Exception, max_tries=3)
def get_webcam_resolution(webcam_config):
    (img_w, img_h) = (640, 360)
    try:
        (_, img_w, img_h) = get_image_info(capture_jpeg(webcam_config, force_stream_url=True))
        _logger.debug(f'Detected webcam resolution - w:{img_w} / h:{img_h}')
    except Exception:
        _logger.exception('Failed to connect to webcam to retrieve resolution. Using default.')

    return (img_w, img_h)


class WebcamStreamer:

    def __init__(self, server_conn, moonrakerconn, app_model, sentry):
        self.server_conn = server_conn
        self.moonrakerconn = moonrakerconn
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

    def start(self, webcams):

        self.shutdown_subprocesses()
        self.close_all_mjpeg_socks()

        moonraker_webcams = (self.moonrakerconn.api_get('server.webcams.list', raise_for_status=False) or {}).get('webcams', [])

        self.webcams = []
        for webcam in webcams:
            moonraker_webcam = next(filter(lambda item: item.get('name') == webcam['name'], moonraker_webcams), None) # Find a Moonraker webcam that matches the name, or None if not found
            if moonraker_webcam is None or not moonraker_webcam.get('enabled'):
                webcam['error'] = '{webcam_name} is not configured in Moonraker, or is disabled.'.format(webcam_name=webcam['name'])
            else:
                webcam['moonraker_config'] = moonraker_webcam

            self.webcams.append(webcam)

        if not self.webcams: # Default webcam list if webcams are not configured in Obico, for legacy users who haven't set up webcam in the new mechanism
            self.webcams = self.default_webcams()

        self.assign_janus_params()
        (janus_bin_path, ld_lib_path) = build_janus_config(self.webcams, self.app_config.server.auth_token, JANUS_WS_PORT, JANUS_ADMIN_WS_PORT, self.sentry)
        self.janus = JanusConn(JANUS_WS_PORT, self.app_config, self.server_conn, self.is_pro, self.sentry)
        self.janus.start(janus_bin_path, ld_lib_path)

        if not self.wait_for_janus():
            for webcam in self.webcams:
                webcam.setdefault('error', 'Janus failed to start')

        for webcam in self.webcams:
            if webcam.get('error'):
                continue    # Skip the one webcam that we have run into issues for

            if webcam['streaming_params']['mode'] == 'h264_rtsp':
                continue    # No extra process is needed when the mode is 'h264_rtsp'
            elif webcam['streaming_params']['mode'] == 'h264_copy':
                self.h264_copy(webcam)
            elif webcam['streaming_params']['mode'] == 'h264_transcode':
                self.h264_transcode(webcam)
            elif webcam['streaming_params']['mode'] == 'mjpeg_webrtc':
                self.mjpeg_webrtc(webcam)

        normalized_webcams = [self.normalized_webcam_dict(webcam) for webcam in self.webcams]
        self.printer_state.set_webcams(normalized_webcams)
        self.server_conn.post_status_update_to_server(with_settings=True)

        return (normalized_webcams, None)  # return value expected for a passthru target

    def shutdown(self):
        self.shutting_down = True
        self.shutdown_subprocesses()
        self.close_all_mjpeg_socks()
        return ('ok', None)  # return value expected for a passthru target

    def list_system_webcams(self):
        moonraker_webcams = (self.moonrakerconn.api_get('server.webcams.list', raise_for_status=False) or {}).get('webcams', [])
        for mr_webcam_config in moonraker_webcams:
            if 'mjpeg' in mr_webcam_config.get('service', '').lower():
                (img_w, img_h) = get_webcam_resolution(mr_webcam_config)
                mr_webcam_config['width'] = img_w
                mr_webcam_config['height'] = img_h

        return (moonraker_webcams, None)


    ## End of passthru target methods


    def assign_janus_params(self):
        first_h264_webcam = next(filter(lambda item: 'h264' in item['streaming_params']['mode'], self.webcams), None)
        if first_h264_webcam:
            first_h264_webcam.setdefault('runtime', {})
            first_h264_webcam['runtime']['stream_id'] = 1  # Set janus id to 1 for the first h264 stream to be compatible with old mobile app versions

        first_mjpeg_webcam = next(filter(lambda item: 'mjpeg' in item['streaming_params']['mode'], self.webcams), None)
        if first_mjpeg_webcam:
            first_mjpeg_webcam.setdefault('runtime', {})
            first_mjpeg_webcam['runtime']['stream_id'] = 2  # Set janus id to 2 for the first mjpeg stream to be compatible with old mobile app versions

        cur_stream_id = 3
        cur_port_num = JANUS_ADMIN_WS_PORT + 1
        for webcam in self.webcams:
            webcam.setdefault('runtime', {})
            if not webcam['runtime'].get('stream_id'):
                webcam['runtime']['stream_id'] = cur_stream_id
                cur_stream_id += 1

            if webcam['streaming_params']['mode'] == 'h264_rtsp':
                 webcam['runtime']['dataport'] = cur_port_num
                 cur_port_num += 1
            elif webcam['streaming_params']['mode'] in ('h264_copy', 'h264_transcode'):
                 webcam['runtime']['videoport'] = cur_port_num
                 cur_port_num += 1
                 webcam['runtime']['videortcpport'] = cur_port_num
                 cur_port_num += 1
                 webcam['runtime']['dataport'] = cur_port_num
                 cur_port_num += 1
            elif webcam['streaming_params']['mode'] == 'mjpeg_webrtc':
                 webcam['runtime']['mjpeg_dataport'] = cur_port_num
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

            h264_http_url =  webcam['streaming_params'].get('h264_http_url')
            rtp_port = webcam['runtime']['videoport']

            # There seems to be a bug in camera-streamer that causes to close .mp4 connection after a random period of time. In that case, we rerun ffmpeg
            self.start_ffmpeg(rtp_port, '-re -i {} -c:v copy'.format(h264_http_url), retry_after_quit=True)
        except Exception as e:
            webcam['error'] = str(e)


    def h264_transcode(self, webcam):

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

        try:
            encoder = h264_encoder()

            webcam_config = webcam['moonraker_config']
            stream_url = webcam_full_url(webcam_config.get('stream_url'))
            if not stream_url:
                raise Exception('stream_url not configured. Unable to stream the webcam.')


            (img_w, img_h) = (parse_integer_or_none(webcam['streaming_params'].get('recode_width')), parse_integer_or_none(webcam['streaming_params'].get('recode_height')))
            if not img_w or not img_h:
                _logger.warn('width and/or height not specified or invalid in streaming parameters. Getting the values from the source.')
                (img_w, img_h) = get_webcam_resolution(webcam_config)

            fps = parse_integer_or_none(webcam['streaming_params'].get('recode_fps'))
            if not fps:
                _logger.warn('FPS not specified or invalid in streaming parameters. Getting the values from the source.')
                fps = webcam_config.target_fps

            bitrate = bitrate_for_dim(img_w, img_h)
            if not self.is_pro:
                fps = min(8, fps) # For some reason, when fps is set to 5, it looks like 2FPS. 8fps looks more like 5
                bitrate = int(bitrate/2)

            rtp_port = webcam['runtime']['videoport']
            self.start_ffmpeg(rtp_port, '-re -i {stream_url} -filter:v fps={fps} -b:v {bitrate} -pix_fmt yuv420p -s {img_w}x{img_h} {encoder}'.format(stream_url=stream_url, fps=fps, bitrate=bitrate, img_w=img_w, img_h=img_h, encoder=encoder))
        except Exception as e:
            webcam['error'] = str(e)


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

            mjpeg_dataport = webcam['runtime']['mjpeg_dataport']

            bandwidth_throttle = 0.004
            if pi_version() == "0":    # If Pi Zero
                bandwidth_throttle *= 2

            mjpeg_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.mjpeg_sock_list.append(mjpeg_sock)

            last_frame_sent = 0

            while True:
                if self.shutting_down:
                    return

                time.sleep( max(last_frame_sent+0.5-time.time(), 0) )  # No more than 1 frame per 0.5 second

                jpg = None
                try:
                    jpg = capture_jpeg(webcam['moonraker_config'])
                except Exception as e:
                    _logger.warning('Failed to capture jpeg - ' + str(e))

                if not jpg:
                    continue

                encoded = base64.b64encode(jpg)
                mjpeg_sock.sendto(bytes('\r\n{}:{}\r\n'.format(len(encoded), len(jpg)), 'utf-8'), (JANUS_SERVER, mjpeg_dataport)) # simple header format for client to recognize
                for chunk in [encoded[i:i+1400] for i in range(0, len(encoded), 1400)]:
                    mjpeg_sock.sendto(chunk, (JANUS_SERVER, mjpeg_dataport))
                    time.sleep(bandwidth_throttle)

            last_frame_sent = time.time()

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
                ffmpeg_pid = int(pid_file.read())
                process_to_kill = subprocess.Process(ffmpeg_pid)
                process_to_kill.terminate()
                process_to_kill.wait(5)
            except Exception as e:
                _logger.warning('Failed to shutdown ffmpeg - ' + str(e))

    def shutdown_subprocesses(self):
        if self.janus:
            self.janus.shutdown()
        self.kill_all_ffmpeg_if_running()

    def close_all_mjpeg_socks(self):
        for mjpeg_sock in self.mjpeg_sock_list:
            mjpeg_sock.close()

    def default_webcams(self):
        # Default webcam list if webcams are not configured in Obico, for legacy users who haven't set up webcam in the new mechanism

        if self.app_config.webcam.disable_video_streaming:
            return []

        moonraker_webcam = self.app_config.webcam.moonraker_webcam_config or {}

        # We need at least stream_url to even start legacy streaming
        if not self.app_config.webcam.stream_url:
            return []

        # In legacy streaming, server gives no streaming parameters. Hence they will all be retrieved from moonraker_webcam. Make sure they are compatibile with the old behavior
        moonraker_webcam.update(dict(
            stream_url=self.app_config.webcam.stream_url,
            snapshot_url=self.app_config.webcam.snapshot_url,
            target_fps=self.app_config.webcam.target_fps,
            flip_horizontal=self.app_config.webcam.flip_h,
            flip_vertical=self.app_config.webcam.flip_v,
            rotation=self.app_config.webcam.rotation,
        ))
        moonraker_webcam.setdefault('service', 'mjpegstreamer-adaptive')

        return [dict(
                name='legacy',
                streaming_params={'mode': 'h264_transcode'},
                moonraker_config=moonraker_webcam,
            )]

    def normalized_webcam_dict(self, webcam):
        return dict(
                name=webcam.get('name'),
                error=webcam.get('error'),
                stream_mode=webcam.get('streaming_params', {}).get('mode'),
                stream_id=webcam.get('runtime', {}).get('stream_id'),
                flipV=webcam['moonraker_config']['flip_vertical'],
                flipH=webcam['moonraker_config']['flip_horizontal'],
                rotation=webcam['moonraker_config']['rotation'],
                streamRatio="16:9"  # TODO: get the actual aspect ratio
                )
