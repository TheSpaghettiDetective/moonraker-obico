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


from .utils import get_image_info, pi_version, get_tags, to_unicode
from .janus import JANUS_SERVER
from .webcam_capture import capture_jpeg

_logger = logging.getLogger('obico.webcam_stream')

FFMPEG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'ffmpeg')
GST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'gst')

PI_CAM_RESOLUTIONS = {
    'low': ((320, 240), (480, 270)),  # resolution for 4:3 and 16:9
    'medium': ((640, 480), (960, 540)),
    'high': ((1296, 972), (1640, 922)),
    'ultra_high': ((1640, 1232), (1920, 1080)),
}


def bitrate_for_dim(img_w, img_h):
    dim = img_w * img_h
    if dim <= 480 * 270:
        return 1000000
    if dim <= 960 * 540:
        return 5000000
    if dim <= 1640 * 922:
        return 20000000
    else:
        return 6000000


def cpu_watch_dog(watched_process, max, interval):

    def watch_process_cpu(watched_process, max, interval):
        while True:
            if not watched_process.is_running():
                return

            cpu_pct = watched_process.cpu_percent(interval=None)
            if cpu_pct > max:
				# TODO: Send notification to user when such thing is available on moonraker
                pass

            time.sleep(interval)

    watch_thread = Thread(target=watch_process_cpu, args=(watched_process, max, interval))
    watch_thread.daemon = True
    watch_thread.start()


class WebcamStreamer:

    def __init__(self, config, sentry):
        self.config = config
        self.sentry = sentry

        self.ffmpeg_proc = None
        self.shutting_down = False


    def video_pipeline(self):
        if not pi_version():
            _logger.warning('Not running on a Pi. Quiting video_pipeline.')
            return

        try:
            self.ffmpeg_from_mjpeg()

        except Exception:
            self.sentry.captureException(tags=get_tags())

            #TODO: sent notification to user
            raise

    def ffmpeg_from_mjpeg(self):
        webcam_config = self.config.webcam

        jpg = capture_jpeg(webcam_config)

        if not jpg:
            _logger.warning('Not a valid jpeg source. Quiting ffmpeg.')
            return

        (_, img_w, img_h) = get_image_info(jpg)
        stream_url = webcam_config.stream_url

        if not stream_url:
            # TODO: notification to user
            return

        self.bitrate = bitrate_for_dim(img_w, img_h)

        self.start_ffmpeg('-re -i {} -b:v {} -pix_fmt yuv420p -s {}x{} -flags:v +global_header -vcodec h264_omx'.format(stream_url, self.bitrate, img_w, img_h))

    def start_ffmpeg(self, ffmpeg_args):
        ffmpeg_cmd = '{} {} -bsf dump_extra -an -f rtp rtp://{}:8004?pkt_size=1300'.format(FFMPEG, ffmpeg_args, JANUS_SERVER)

        _logger.debug('Popen: {}'.format(ffmpeg_cmd))
        FNULL = open(os.devnull, 'w')
        self.ffmpeg_proc = psutil.Popen(ffmpeg_cmd.split(' '), stdin=subprocess.PIPE, stdout=FNULL, stderr=subprocess.PIPE)
        self.ffmpeg_proc.nice(10)

        cpu_watch_dog(self.ffmpeg_proc, max=80, interval=20)

        def monitor_ffmpeg_process():  # It's pointless to restart ffmpeg without calling pi_camera.record with the new input. Just capture unexpected exits not to see if it's a big problem
            ring_buffer = deque(maxlen=50)
            while True:
                err = to_unicode(self.ffmpeg_proc.stderr.readline(), errors='replace')
                if not err:  # EOF when process ends?
                    if self.shutting_down:
                        return

                    returncode = self.ffmpeg_proc.wait()
                    msg = 'STDERR:\n{}\n'.format('\n'.join(ring_buffer))
                    _logger.error(msg)
                    self.sentry.captureMessage('ffmpeg quit! This should not happen. Exit code: {}'.format(returncode), tags=get_tags())
                    return
                else:
                    ring_buffer.append(err)

        ffmpeg_thread = Thread(target=monitor_ffmpeg_process)
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
