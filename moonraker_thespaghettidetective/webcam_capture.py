from __future__ import absolute_import
from io import StringIO
import re
from urllib.request import urlopen
from urllib.parse import urlparse
from contextlib import closing
import requests
import backoff


def webcam_full_url(url):
    if not url or not url.strip():
        return None

    full_url = url.strip()
    if not urlparse(full_url).scheme:
        full_url = 'http://localhost/' + re.sub(r'^\/', '', full_url)

    return full_url


@backoff.on_exception(backoff.expo, Exception, max_tries=3)
@backoff.on_predicate(backoff.expo, max_tries=3)
def capture_jpeg(webcam_config):
    snapshot_url = webcam_full_url(
        webcam_config.snapshot_url or ''
    )

    if snapshot_url:
        snapshot_validate_ssl = webcam_config.snapshot_ssl_validation

        r = requests.get(snapshot_url, stream=True, timeout=5,
                         verify=snapshot_validate_ssl)
        r.raise_for_status()
        jpg = r.content

        return jpg
    else:
        stream_url = webcam_full_url(
            webcam_config.stream_url or '/webcam/?action=stream'
        )

        with closing(urlopen(stream_url)) as res:
            chunker = MjpegStreamChunker()

            while True:
                data = res.readline()
                mjpg = chunker.findMjpegChunk(data)
                if mjpg:
                    res.close()

                    mjpeg_headers_index = mjpg.find('\r\n'*2)
                    if mjpeg_headers_index > 0:
                        return mjpg[mjpeg_headers_index+4:]
                    else:
                        raise Exception('wrong mjpeg data format')


class MjpegStreamChunker:

    def __init__(self):
        self.boundary = None
        self.current_chunk = StringIO()

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
