from __future__ import absolute_import
import base64
import io
import re
import os
from urllib.request import urlopen
from urllib.parse import urlparse
from urllib.error import HTTPError
from contextlib import closing
import requests
import backoff
import logging
import time
import threading

from .utils import DEBUG

POST_PIC_INTERVAL_SECONDS = 10.0
if DEBUG:
    POST_PIC_INTERVAL_SECONDS = 3.0

_logger = logging.getLogger('obico.webcam_capture')


@backoff.on_exception(backoff.expo, Exception, max_tries=3)
@backoff.on_predicate(backoff.expo, max_tries=3)
def capture_jpeg(webcam_config, force_stream_url=False):
    MAX_JPEG_SIZE = 7000000

    snapshot_url = webcam_config.snapshot_url
    if snapshot_url and not force_stream_url:
        r = requests.get(snapshot_url, stream=True, timeout=5, verify=False)
        r.raise_for_status()

        response_content = b''
        start_time = time.monotonic()
        for chunk in r.iter_content(chunk_size=1024):
            response_content += chunk
            if len(response_content) > MAX_JPEG_SIZE:
                r.close()
                raise Exception('Payload returned from the snapshot_url is too large. Did you configure stream_url as snapshot_url?')

        r.close()
        return response_content

    else:
        stream_url = webcam_config.stream_url
        if not stream_url:
            raise ValueError('Invalid snapshot URL or stream URL in webcam setting: "{}"'.format(webcam_config))

        with closing(urlopen(stream_url)) as res:
            chunker = MjpegStreamChunker()

            data_bytes = 0
            while True:
                data = res.readline()
                data_bytes += len(data)
                if data == b'':
                    raise ValueError('End of stream before a valid jpeg is found')
                if data_bytes > MAX_JPEG_SIZE:
                    raise ValueError('Reached the size cap before a valid jpeg is found.')

                mjpg = chunker.findMjpegChunk(data)
                if mjpg:
                    res.close()

                    mjpeg_headers_index = mjpg.find(b'\r\n'*2)
                    if mjpeg_headers_index > 0:
                        return mjpg[mjpeg_headers_index+4:]
                    else:
                        raise ValueError('Wrong mjpeg data format')


class MjpegStreamChunker:

    def __init__(self):
        self.boundary = None
        self.current_chunk = io.BytesIO()

    def findMjpegChunk(self, line):
        # Return: mjpeg chunk if found
        #         None: in the middle of the chunk
        # The first time endOfChunk should be called
        # with 'boundary' text as input
        if not len(line.strip()): # don't parse empty lines as the boundary
            self.current_chunk.write(line)
            return None

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

    def pic_post_loop(self):
        while True:
            try:
                viewing_boost = self.need_viewing_boost.wait(1)
                if viewing_boost:
                    self.need_viewing_boost.clear()
                    repeats = 3 if self.app_model.linked_printer.get('is_pro') else 1 # Pro users get better viewing boost
                    for _ in range(repeats):
                        self.server_conn.post_pic_to_server(webcam_config=self.config.primary_webcam_config, viewing_boost=True)
                    continue

                if not self.app_model.printer_state.is_printing():
                    continue

                interval_seconds = POST_PIC_INTERVAL_SECONDS
                if not self.app_model.remote_status['viewing'] and not self.app_model.remote_status['should_watch']:
                    interval_seconds *= 12      # Slow down jpeg posting if needed

                if self.last_jpg_post_ts > time.time() - interval_seconds:
                    continue

                self.last_jpg_post_ts = time.time()
                self.server_conn.post_pic_to_server(webcam_config=self.config.primary_webcam_config, viewing_boost=False)
            except:
                self.sentry.captureException()

    def web_snapshot_request(self, url):
        class SnapshotConfig:
            def __init__(self, snapshot_url):
                self.snapshot_url = snapshot_url

        snapshot = capture_jpeg(SnapshotConfig(url))
        base64_image = base64.b64encode(snapshot).decode('utf-8')
        return {'pic': base64_image}, None
