import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from moonraker_obico.config import Config


VALID_CONFIG = '''\
[server]
url = https://example.test

[moonraker]
host = moonraker
port = 7125

[webcam]
disable_video_streaming = False

[logging]
level = INFO

[tunnel]
'''


class ConfigTests(unittest.TestCase):

    def test_empty_reload_keeps_last_valid_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, 'moonraker-obico.cfg')
            with open(config_path, 'w') as config_file:
                config_file.write(VALID_CONFIG)

            config = Config(config_path)
            config.load_from_config_file()

            with open(config_path, 'w'):
                pass
            with self.assertLogs('obico.config', level='WARNING'):
                config.load_from_config_file()

            self.assertEqual('https://example.test', config.server.url)
            self.assertEqual(
                ['server', 'moonraker', 'webcam', 'logging', 'tunnel'],
                config._config.sections(),
            )
            with open(config_path) as config_file:
                self.assertEqual('', config_file.read())

    def test_empty_config_is_rejected_on_initial_load(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, 'moonraker-obico.cfg')
            with open(config_path, 'w'):
                pass

            with self.assertRaisesRegex(ValueError, 'No config sections found'):
                Config(config_path).load_from_config_file()

    def test_write_atomically_replaces_config_and_preserves_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, 'moonraker-obico.cfg')
            with open(config_path, 'w') as config_file:
                config_file.write(VALID_CONFIG)
            os.chmod(config_path, 0o640)

            config = Config(config_path)
            config.load_from_config_file()
            with open(config_path) as config_file:
                original_contents = config_file.read()
            real_replace = os.replace
            replace_calls = []

            def observe_replace(source, destination):
                self.assertEqual(config_path, destination)
                with open(destination) as destination_file:
                    self.assertEqual(original_contents, destination_file.read())
                with open(source) as staged_file:
                    self.assertIn('auth_token = new-token', staged_file.read())
                replace_calls.append((source, destination))
                real_replace(source, destination)

            with patch('moonraker_obico.config.os.replace', side_effect=observe_replace):
                config.update_server_auth_token('new-token')

            self.assertEqual(1, len(replace_calls))
            self.assertEqual(0o640, stat.S_IMODE(os.stat(config_path).st_mode))
            with open(config_path) as config_file:
                self.assertIn('auth_token = new-token', config_file.read())


if __name__ == '__main__':
    unittest.main()
