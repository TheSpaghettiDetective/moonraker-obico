import threading
import time
import traceback
import unittest
from unittest import mock

from moonraker_obico import server_conn


class RaceWithWebSocketCloseTests(unittest.TestCase):
    """The websocket thread clears ServerConn.ss when the socket closes, at any moment.

    The send loop must not look at ServerConn.ss twice (once to check it, once to send on it),
    because the close callback can run in between and the send would hit None.
    """

    def run_loop_with_close_between_check_and_send(self):
        in_check = threading.Event()
        closed = threading.Event()
        clients = []
        sent = []
        reported = []

        class FakeWebSocketClient:
            def __init__(self, url, header=None, on_ws_msg=None, on_ws_close=None, on_ws_open=None, **kwargs):
                self.ws = object()
                self.on_ws_close = on_ws_close
                self.connected_calls = 0
                clients.append(self)

            def connected(self):
                self.connected_calls += 1
                if self.connected_calls == 1:
                    # The send loop is inside its `is the client connected?` check. Let the websocket
                    # thread's close callback run right now, before the loop goes on to send.
                    in_check.set()
                    self.test_waits_for_close()
                return True

            def test_waits_for_close(self):
                assert closed.wait(5), 'the close callback never ran'

            def send(self, raw, as_binary=False):
                sent.append(raw)

        class Sentry:
            def captureException(self, *args, **kwargs):
                reported.append(traceback.format_exc())

        def websocket_thread():
            assert in_check.wait(5)
            client = clients[0]
            client.on_ws_close(client.ws, close_status_code=1006)
            closed.set()

        config = mock.MagicMock()
        config.server.auth_token = 'token'
        config.server.ws_url.return_value = 'wss://example.invalid/ws/dev/'
        conn = server_conn.ServerConn(config, mock.MagicMock(), lambda msg: None, Sentry())

        with mock.patch.object(server_conn, 'WebSocketClient', FakeWebSocketClient):
            threading.Thread(target=websocket_thread, daemon=True).start()
            loop = threading.Thread(target=conn.start, daemon=True)
            loop.start()  # start() queues an initial message, which makes the loop create the client
            conn.send_ws_msg_to_server({'second': 'message'})  # reaches the check with a client present
            conn.send_ws_msg_to_server({'third': 'message'})
            deadline = time.monotonic() + 5
            while len(sent) < 3 and not reported and time.monotonic() < deadline:
                time.sleep(0.01)
            conn.should_reconnect = False
            conn.send_ws_msg_to_server({})
            loop.join(5)

        return sent, reported

    def test_close_between_connected_check_and_send_does_not_raise(self):
        sent, reported = self.run_loop_with_close_between_check_and_send()

        self.assertEqual(reported, [])
        self.assertGreaterEqual(len(sent), 3)


    def test_close_right_after_a_new_client_is_created_does_not_raise(self):
        sent = []
        reported = []

        class FakeWebSocketClient:
            def __init__(self, url, header=None, on_ws_msg=None, on_ws_close=None, on_ws_open=None, **kwargs):
                self.ws = object()
                self.on_ws_close = on_ws_close

            def connected(self):
                return True

            def send(self, raw, as_binary=False):
                sent.append(raw)

        class Sentry:
            def captureException(self, *args, **kwargs):
                reported.append(traceback.format_exc())

        class ConnThatLosesNewClientImmediately(server_conn.ServerConn):
            """Runs the websocket thread's close callback the moment a client is stored in self.ss."""

            @property
            def ss(self):
                return self._ss

            @ss.setter
            def ss(self, client):
                self._ss = client
                if client is not None:
                    client.on_ws_close(client.ws, close_status_code=1006)

        config = mock.MagicMock()
        config.server.auth_token = 'token'
        config.server.ws_url.return_value = 'wss://example.invalid/ws/dev/'
        conn = ConnThatLosesNewClientImmediately(config, mock.MagicMock(), lambda msg: None, Sentry())

        with mock.patch.object(server_conn, 'WebSocketClient', FakeWebSocketClient):
            loop = threading.Thread(target=conn.start, daemon=True)
            loop.start()  # start() queues an initial message, which makes the loop create the client
            deadline = time.monotonic() + 5
            while not sent and not reported and time.monotonic() < deadline:
                time.sleep(0.01)
            conn.should_reconnect = False
            conn.send_ws_msg_to_server({})
            loop.join(5)

        self.assertEqual(reported, [])
        self.assertGreaterEqual(len(sent), 1)


if __name__ == '__main__':
    unittest.main()
