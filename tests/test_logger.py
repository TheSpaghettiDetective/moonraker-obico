import logging
import os
import tempfile
import unittest
from types import SimpleNamespace

from moonraker_obico.logger import setup_logging


class LoggerRedactionTests(unittest.TestCase):

    def test_setup_logging_redacts_application_and_dependency_records(self):
        root_logger = logging.getLogger()
        original_handlers = root_logger.handlers[:]
        original_level = root_logger.level
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = os.path.join(temp_dir, 'moonraker-obico.log')
                config = SimpleNamespace(
                    path=log_path,
                    level='DEBUG',
                    log_network=True,
                )
                setup_logging(config)

                logging.getLogger('obico.integration').info(
                    'config=%s',
                    {
                        'auth_token': 'settings-secret',
                        'user_id': 'user-123',
                        'mode': 'debug',
                    },
                )
                logging.getLogger('urllib3.connectionpool').debug(
                    'GET %s',
                    'https://camera:camera-secret@example.test/live?token=query-secret&fps=10',
                )
                try:
                    raise ValueError('password=traceback-secret')
                except ValueError:
                    logging.getLogger('obico.integration').exception('request failed')

                for handler in root_logger.handlers:
                    handler.flush()
                with open(log_path, encoding='utf-8') as log_file:
                    output = log_file.read()
        finally:
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
                handler.close()
            for handler in original_handlers:
                root_logger.addHandler(handler)
            root_logger.setLevel(original_level)

        for secret in (
            'settings-secret',
            'camera-secret',
            'query-secret',
            'traceback-secret',
        ):
            self.assertNotIn(secret, output)
        for safe_value in ('user-123', 'mode', 'fps=10', '<redacted>'):
            self.assertIn(safe_value, output)


if __name__ == '__main__':
    unittest.main()
