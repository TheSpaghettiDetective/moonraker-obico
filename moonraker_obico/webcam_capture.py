from __future__ import absolute_import
import io
import re
from urllib.request import urlopen
from urllib.parse import urlparse
from urllib.error import URLError, HTTPError
from contextlib import closing
import requests
import backoff
import logging


_logger = logging.getLogger('obico.webcam_capture')


@backoff.on_exception(backoff.expo, Exception, max_tries=3)
@backoff.on_predicate(backoff.expo, max_tries=3)
def capture_jpeg(webcam_config):
    snapshot_url = webcam_config.snapshot_url

    if snapshot_url:
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
