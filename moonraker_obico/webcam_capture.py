from __future__ import absolute_import
import io
import re
import os
from urllib.request import urlopen
from urllib.parse import urlparse
from urllib.error import URLError, HTTPError
from contextlib import closing
import requests
import backoff
import logging
import time
import threading

POST_PIC_INTERVAL_SECONDS = 10.0
if os.environ.get('DEBUG'):
    POST_PIC_INTERVAL_SECONDS = 3.0

_logger = logging.getLogger('obico.webcam_capture')


@backoff.on_exception(backoff.expo, Exception, max_tries=3)
@backoff.on_predicate(backoff.expo, max_tries=3)
def capture_jpeg(webcam_config, force_stream_url=False):
    snapshot_url = webcam_config.snapshot_url

    if snapshot_url and not force_stream_url:
        snapshot_validate_ssl = webcam_config.snapshot_ssl_validation

        _logger.debug(f'GET {snapshot_url}')
        r = requests.get(snapshot_url, stream=True, timeout=5,
                         verify=snapshot_validate_ssl)
        if not r.ok:
            _logger.warn('Error taking from jpeg source: {}'.format(snapshot_url))
            return

        return r.content

    else:
        stream_url = webcam_config.stream_url
        if not stream_url:
            return

        _logger.debug(f'GET {stream_url}')
        try:
            with closing(urlopen(stream_url)) as res:
                chunker = MjpegStreamChunker()

                while True:
                    data = res.readline()
                    mjpg = chunker.findMjpegChunk(data)
                    if mjpg:
                        res.close()

                        mjpeg_headers_index = mjpg.find(b'\r\n'*2)
                        if mjpeg_headers_index > 0:
                            return mjpg[mjpeg_headers_index+4:]
                        else:
                            _logger.warn('wrong mjpeg data format')
                            return
        except (URLError, HTTPError):
            _logger.warn('Error taking from mjpeg source: {}'.format(stream_url))
            return


class MjpegStreamChunker:

    def __init__(self):
        self.boundary = None
        self.current_chunk = io.BytesIO()

    def findMjpegChunk(self, line):
        # Return: mjpeg chunk if found
        #         None: in the middle of the chunk
        # The first time endOfChunk should be called
        # with 'boundary' text as input
        if not self.boundary:
            self.boundary = line
            self.current_chunk.write(line)
            return None

        if len(line) == len(self.boundary) and line == self.boundary:
            # start of next chunk
            return self.current_chunk.getvalue()

        self.current_chunk.write(line)
        return None


class JpegPoster:

    def __init__(self, app_model, server_conn, sentry):
        self.config = app_model.config
        self.app_model = app_model
        self.server_conn = server_conn
        self.sentry = sentry
        self.last_jpg_post_ts = 0
        self.need_viewing_boost = threading.Event()


    def post_pic_to_server(self, viewing_boost=False):
        files = {'pic': capture_jpeg(self.config.webcam)}
        data = {'viewing_boost': 'true'} if viewing_boost else {}

        resp = self.server_conn.send_http_request('POST', '/api/v1/octo/pic/', timeout=60, files=files, data=data, raise_exception=True)
        _logger.debug('Jpeg posted to server - viewing_boost: {0} - {1}'.format(viewing_boost, resp))

    def pic_post_loop(self):
        while True:
            try:
                viewing_boost = self.need_viewing_boost.wait(1)
                if viewing_boost:
                    self.need_viewing_boost.clear()
                    repeats = 3 if self.app_model.linked_printer.get('is_pro') else 1 # Pro users get better viewing boost
                    for _ in range(repeats):
                        self.post_pic_to_server(viewing_boost=True)
                    continue

                if not self.app_model.printer_state.is_printing():
                    continue

                interval_seconds = POST_PIC_INTERVAL_SECONDS
                if not self.app_model.remote_status['viewing'] and not self.app_model.remote_status['should_watch']:
                    interval_seconds *= 12      # Slow down jpeg posting if needed

                if self.last_jpg_post_ts > time.time() - interval_seconds:
                    continue

                self.last_jpg_post_ts = time.time()
                self.post_pic_to_server(viewing_boost=False)
            except:
                self.sentry.captureException()
