import unittest
from unittest import mock

from moonraker_obico.app import App
from moonraker_obico.printer import PrinterState


class StatusToClientTests(unittest.TestCase):
    """The page gets status from the server and, every 2 seconds, straight from the plugin.

    Both must say the same thing, or the page flips between them.
    """

    def app_with_printer_state(self, status, current_print_ts):
        app_config = mock.Mock()
        app_config.all_mr_heaters.return_value = []
        printer_state = PrinterState(app_config)
        printer_state.status = status
        printer_state.current_print_ts = current_print_ts

        app = App.__new__(App)
        app.model = mock.Mock(printer_state=printer_state)
        app.client_conn = mock.Mock()
        return app, printer_state

    def test_klipper_offline_sends_the_page_nothing_like_the_server(self):
        app, printer_state = self.app_with_printer_state(status={}, current_print_ts=None)

        app.post_printer_status_to_client()

        self.assertNotIn('status', printer_state.to_dict())
        app.client_conn.send_msg_to_client.assert_not_called()

    def test_klipper_ready_sends_the_page_the_server_status(self):
        app, printer_state = self.app_with_printer_state(
            status={'webhooks': {'state': 'ready'}, 'print_stats': {'state': 'standby'}}, current_print_ts=-1)

        app.post_printer_status_to_client()

        sent = app.client_conn.send_msg_to_client.call_args[0][0]
        self.assertEqual(sent['status']['state']['text'], PrinterState.STATE_OPERATIONAL)
        self.assertEqual(sent['status']['state'], printer_state.to_dict()['status']['state'])


if __name__ == '__main__':
    unittest.main()
