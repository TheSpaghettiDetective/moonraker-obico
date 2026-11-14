"""The agent must notice a silently dead connection to the server.

A firewall reboot or NAT state flush drops the TCP connection without sending a FIN or RST
to the agent. Without websocket pings the client socket keeps reporting `connected` and
the agent never reconnects. These tests put a proxy between the client and a real
websocket server, make the proxy swallow traffic, and check the client notices.

The `websockets` package provides that test server. It is declared in
requirements-test.txt, not requirements.txt, so it is not installed on printers:
    pip install -r requirements.txt -r requirements-test.txt
"""
import asyncio
import socket
import threading
import time
import unittest

import websockets

from moonraker_obico.ws import WebSocketClient


class BlackholeProxy:
    """TCP proxy that can switch to silently dropping traffic in both directions."""

    def __init__(self, upstream_port):
        self.upstream_port = upstream_port
        self.blackholed = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        client, _ = self.listener.accept()
        upstream = socket.create_connection(('127.0.0.1', self.upstream_port))
        threading.Thread(target=self._pump, args=(client, upstream), daemon=True).start()
        threading.Thread(target=self._pump, args=(upstream, client), daemon=True).start()

    def _pump(self, src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    return
                if not self.blackholed.is_set():
                    dst.sendall(data)
        except OSError:
            return


class WebSocketServer:
    """Real websocket server on a background event loop; answers pings automatically."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        async def handler(ws, *args):
            async for _ in ws:
                pass

        async def main():
            # The server must not ping on its own; only the client's pings are under test.
            self.server = await websockets.serve(handler, '127.0.0.1', 0, ping_interval=None)
            self.port = self.server.sockets[0].getsockname()[1]
            ready.set()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(main())
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        assert ready.wait(5), 'websocket server did not start'


class DeadPeerDetectionTests(unittest.TestCase):

    def connect_through_blackhole_proxy(self, **client_kwargs):
        server = WebSocketServer()
        proxy = BlackholeProxy(server.port)
        closed = threading.Event()
        client = WebSocketClient(
            'ws://127.0.0.1:{}/'.format(proxy.port),
            on_ws_close=lambda ws, close_status_code: closed.set(),
            waitsecs=5,
            **client_kwargs)
        self.addCleanup(client.close)
        self.assertTrue(client.connected())
        return client, proxy, closed

    def test_silently_dropped_connection_is_closed_by_ping_timeout(self):
        client, proxy, closed = self.connect_through_blackhole_proxy(ping_interval=1, ping_timeout=0.5)

        proxy.blackholed.set()

        self.assertTrue(closed.wait(5), 'client never noticed the dead connection')
        self.assertFalse(client.connected())

    def test_healthy_connection_survives_pings(self):
        client, proxy, closed = self.connect_through_blackhole_proxy(ping_interval=1, ping_timeout=0.5)

        time.sleep(3.5)  # several ping/pong rounds

        self.assertFalse(closed.is_set())
        self.assertTrue(client.connected())

    def test_without_pings_a_dropped_connection_goes_unnoticed(self):
        """Documents the original bug and that pings stay opt-in for local connections."""
        client, proxy, closed = self.connect_through_blackhole_proxy()

        proxy.blackholed.set()
        client.send('{}')

        self.assertFalse(closed.wait(3))
        self.assertTrue(client.connected())

    def test_rejects_ping_timeout_not_below_interval(self):
        for timeout in (None, 0, 5, 10):
            with self.assertRaises(ValueError):
                WebSocketClient('ws://127.0.0.1:1/', ping_interval=5, ping_timeout=timeout)


class ServerConnEnablesPingsTests(unittest.TestCase):

    def test_server_websocket_is_built_with_pings(self):
        from unittest import mock
        from moonraker_obico import server_conn

        built = []

        class StopLoop(BaseException):
            pass

        def fake_client(url, **kwargs):
            built.append(kwargs)
            raise StopLoop()

        config = mock.MagicMock()
        config.server.auth_token = 'token'
        conn = server_conn.ServerConn(config, mock.MagicMock(), lambda msg: None, mock.MagicMock())
        with mock.patch.object(server_conn, 'WebSocketClient', side_effect=fake_client):
            with self.assertRaises(StopLoop):
                conn.start()

        self.assertEqual(built[0]['ping_interval'], server_conn.SERVER_WS_PING_INTERVAL_SECONDS)
        self.assertEqual(built[0]['ping_timeout'], server_conn.SERVER_WS_PING_TIMEOUT_SECONDS)
        self.assertGreater(server_conn.SERVER_WS_PING_INTERVAL_SECONDS, server_conn.SERVER_WS_PING_TIMEOUT_SECONDS)


if __name__ == '__main__':
    unittest.main()
