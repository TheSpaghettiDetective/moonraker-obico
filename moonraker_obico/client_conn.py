import bson
import logging
import json
import socket
import threading
import time
import sys
import zlib
import re
from collections import deque

from .janus import JANUS_SERVER

__python_version__ = 3 if sys.version_info >= (3, 0) else 2

_logger = logging.getLogger('obico.app')
MAX_PAYLOAD_SIZE = 1500 # 1500 bytes is the max size of a UDP packet

class ClientConn:

    def __init__(self):
        self.printer_data_channel_conn = None

    def open_data_channel(self, port):
        self.printer_data_channel_conn = DataChannelConn(JANUS_SERVER, port)

    def send_msg_to_client(self, data):
        if self.printer_data_channel_conn is None:
            return

        payload = json.dumps(data, default=str).encode('utf8')
        if __python_version__ == 3:
            compressor  = zlib.compressobj(
                level=zlib.Z_DEFAULT_COMPRESSION, method=zlib.DEFLATED,
                wbits=15, memLevel=8, strategy=zlib.Z_DEFAULT_STRATEGY)
        else:
            # no kw args
            compressor  = zlib.compressobj(
                zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, 15, 8, zlib.Z_DEFAULT_STRATEGY)

        compressed_data = compressor.compress(payload)
        compressed_data += compressor.flush()

        self.printer_data_channel_conn.send(compressed_data)

    def close(self):
        if self.printer_data_channel_conn:
            self.printer_data_channel_conn.close()


class DataChannelConn(object):

    def __init__(self, addr, port):
        self.addr = addr
        self.port = port
        self.sock = None
        self.sock_lock = threading.RLock()

    def send(self, payload):
        if len(payload) > MAX_PAYLOAD_SIZE:
            _logger.debug('datachannel payload too big (%s)' % (len(payload), ))
            return

        with self.sock_lock:
            if self.sock is None:
                try:
                    self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                except OSError as ex:
                    _logger.warning('could not open udp socket (%s)' % ex)

            if self.sock is not None:
                try:
                    self.sock.sendto(payload, (self.addr, self.port))
                except socket.error as ex:
                    _logger.warning(
                        'could not send to janus datachannel (%s)' % ex)
                except OSError as ex:
                    _logger.warning('udp socket might be closed (%s)' % ex)
                    self.sock = None

    def close(self):
        with self.sock_lock:
            self.sock.close()
            self.sock = None
