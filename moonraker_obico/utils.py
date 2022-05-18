from __future__ import absolute_import
from typing import Dict, Optional
import dataclasses
import time
import re
import os
import random
import platform
import logging
import tempfile
from io import BytesIO
import struct
import threading
import socket
from contextlib import closing
from typing import Union
from sarge import run, Capture
from pathvalidate import sanitize_filename as sfn
import backoff
import requests

_logger = logging.getLogger('obico.utils')

DEBUG = os.environ.get('DEBUG')
CAM_EXCLUSIVE_USE = os.path.join(tempfile.gettempdir(), '.using_picam')

# Update printer settings at max 30 minutes interval,
# as they are relatively static.



class ShutdownException(Exception):
    pass


class FlowError(Exception):

    def __init__(self, message, exc=None):
        self.exc = exc
        super().__init__(message)


class FlowTimeout(Exception):
    pass


class FatalError(Exception):
    def __init__(self, message, exc=None):
        self.exc = exc
        super().__init__(message)


class AuthenticationError(Exception):

    def __init__(self, message, exc=None):
        self.exc = exc
        super().__init__(message)


@dataclasses.dataclass
class Event:
    name: str
    data: Dict
    sender: Optional[str] = None


def resp_to_exception(resp: requests.Response) -> Optional[Exception]:
    try:
        resp.raise_for_status()
    except Exception as exc:
        return exc


def sanitize_filename(fname):
    if "/" in fname or "\\" in fname:
        raise ValueError("name must not contain / or \\")

    result = sfn(fname)

    return result.lstrip(".")


class ExpoBackoff:

    def __init__(self, max_seconds, max_attempts=3):
        self.attempts = 0
        self.max_seconds = max_seconds
        self.max_attempts = max_attempts

    def reset(self):
        self.attempts = 0

    def more(self, e):
        self.attempts += 1
        if (
            self.max_attempts is not None and
            self.attempts >= self.max_attempts
        ):
            _logger.error('giving up on error: %s' % (e))
            raise e
        else:
            delay = self.get_delay(self.attempts, self.max_seconds)
            _logger.error('backing off %f seconds: %s' % (delay, e))

            time.sleep(delay)

    @classmethod
    def get_delay(cls, attempts, max_seconds):
        delay = 2 ** (attempts - 3)
        if delay > max_seconds:
            delay = max_seconds
        delay *= 0.5 + random.random()
        return delay


class SentryWrapper:

    def __init__(self, sentryClient):
        self.sentryClient = sentryClient

    def captureException(self, *args, **kwargs):
        if self.sentryClient:
            self.sentryClient.captureException(*args, **kwargs)

    def user_context(self, *args, **kwargs):
        if self.sentryClient:
            self.sentryClient.user_context(*args, **kwargs)

    def tags_context(self, *args, **kwargs):
        if self.sentryClient:
            self.sentryClient.tags_context(*args, **kwargs)

    def captureMessage(self, *args, **kwargs):
        if self.sentryClient:
            self.sentryClient.captureMessage(*args, **kwargs)


def pi_version():
    try:
        with open(
            '/sys/firmware/devicetree/base/model', 'r'
        ) as firmware_model:
            model = re.search('Raspberry Pi(.*)',
                              firmware_model.read()).group(1)
            if model:
                return "0" if re.search('Zero', model, re.IGNORECASE) else "3"
            else:
                return None
    except Exception:
        return None


system_tags = None
tags_mutex = threading.RLock()


def get_tags():
    global system_tags, tags_mutex

    with tags_mutex:
        if system_tags:
            return system_tags

    (os, _, ver, _, arch, _) = platform.uname()
    tags = dict(os=os, os_ver=ver, arch=arch)
    try:
        v4l2 = run('v4l2-ctl --list-devices', stdout=Capture())
        v4l2_out = ''.join(
            re.compile(
                r"^([^\t]+)", re.MULTILINE
            ).findall(
                v4l2.stdout.text
            )
        ).replace('\n', '')
        if v4l2_out:
            tags['v4l2'] = v4l2_out
    except Exception:
        pass

    try:
        usb = run(
            "lsusb | cut -d ' ' -f 7- | grep -vE ' hub|"
            "Hub' | grep -v 'Standard Microsystems Corp'",
            stdout=Capture())
        usb_out = ''.join(usb.stdout.text).replace('\n', '')
        if usb_out:
            tags['usb'] = usb_out
    except Exception:
        pass

    with tags_mutex:
        system_tags = tags
        return system_tags


def not_using_pi_camera():
    try:
        os.remove(CAM_EXCLUSIVE_USE)
    except Exception:
        pass


def using_pi_camera():
    # touch CAM_EXCLUSIVE_USE to indicate the
    # intention of exclusive use of pi camera
    open(CAM_EXCLUSIVE_USE, 'a').close()


def get_image_info(data):
    data_bytes = data
    if not isinstance(data, str):
        data = data.decode('iso-8859-1')
    size = len(data)
    height = -1
    width = -1
    content_type = ''

    # handle GIFs
    if (size >= 10) and data[:6] in ('GIF87a', 'GIF89a'):
        # Check to see if content_type is correct
        content_type = 'image/gif'
        w, h = struct.unpack("<HH", data[6:10])
        width = int(w)
        height = int(h)

    # See PNG 2. Edition spec (http://www.w3.org/TR/PNG/)
    # Bytes 0-7 are below, 4-byte chunk length, then 'IHDR'
    # and finally the 4-byte width, height
    elif ((size >= 24) and data.startswith('\211PNG\r\n\032\n')
          and (data[12:16] == 'IHDR')):
        content_type = 'image/png'
        w, h = struct.unpack(">LL", data[16:24])
        width = int(w)
        height = int(h)

    # Maybe this is for an older PNG version.
    elif (size >= 16) and data.startswith('\211PNG\r\n\032\n'):
        # Check to see if we have the right content type
        content_type = 'image/png'
        w, h = struct.unpack(">LL", data[8:16])
        width = int(w)
        height = int(h)

    # handle JPEGs
    elif (size >= 2) and data.startswith('\377\330'):
        content_type = 'image/jpeg'
        jpeg = BytesIO(data_bytes)
        jpeg.read(2)
        b = jpeg.read(1)
        try:
            while (b and ord(b) != 0xDA):
                while (ord(b) != 0xFF):
                    b = jpeg.read(1)
                while (ord(b) == 0xFF):
                    b = jpeg.read(1)
                if (ord(b) >= 0xC0 and ord(b) <= 0xC3):
                    jpeg.read(3)
                    h, w = struct.unpack(">HH", jpeg.read(4))
                    break
                else:
                    jpeg.read(int(struct.unpack(">H", jpeg.read(2))[0])-2)
                b = jpeg.read(1)
            width = int(w)
            height = int(h)
        except struct.error:
            pass
        except ValueError:
            pass

    return content_type, width, height


@backoff.on_exception(backoff.expo, Exception, max_tries=3, jitter=None)
@backoff.on_predicate(backoff.expo, max_tries=3, jitter=None)
def wait_for_port(host, port):
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        return sock.connect_ex((host, port)) == 0


def wait_for_port_to_close(host, port):
    for i in range(10):   # Wait for up to 5s
        with closing(
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ) as sock:
            if sock.connect_ex((host, port)) != 0:  # Port is not open
                return
            time.sleep(0.5)


def raise_for_status(resp, with_content=False, **kwargs):
    # puts reponse content into exception
    if with_content:
        try:
            resp.raise_for_status()
        except Exception as exc:
            args = exc.args
            if not args:
                arg0 = ''
            else:
                arg0 = args[0]
            arg0 = "{} {}".format(arg0, resp.text)
            exc.args = (arg0, ) + args[1:]
            exc.kwargs = kwargs

            raise
    resp.raise_for_status()


# Courtesy of https://github.com/OctoPrint/OctoPrint/blob/master/src/octoprint/util/__init__.py

def to_unicode(
    s_or_u: Union[str, bytes], encoding: str = "utf-8", errors: str = "strict"
) -> str:
    """
    Make sure ``s_or_u`` is a unicode string (str).
    Arguments:
        s_or_u (str or bytes): The value to convert
        encoding (str): encoding to use if necessary, see :meth:`python:bytes.decode`
        errors (str): error handling to use if necessary, see :meth:`python:bytes.decode`
    Returns:
        str: converted string.
    """
    if s_or_u is None:
        return s_or_u

    if not isinstance(s_or_u, (str, bytes)):
        s_or_u = str(s_or_u)

    if isinstance(s_or_u, bytes):
        return s_or_u.decode(encoding, errors=errors)
    else:
        return s_or_u


def pi_version():
    try:
        with open('/sys/firmware/devicetree/base/model', 'r') as firmware_model:
            model = re.search('Raspberry Pi(.*)', firmware_model.read()).group(1)
            if model:
                return "0" if re.search('Zero', model, re.IGNORECASE) else "3"
            else:
                return None
    except:
        return None

