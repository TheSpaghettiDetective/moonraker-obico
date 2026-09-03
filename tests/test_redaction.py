import copy
import importlib.util
import logging
import os
import sys
import unittest


MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'moonraker_obico',
    'redaction.py',
)
SPEC = importlib.util.spec_from_file_location('obico_redaction', MODULE_PATH)
redaction = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(redaction)


class DummyRequest(object):
    method = 'POST'
    url = 'https://camera:camera-password@example.com/live?token=url-secret&fps=10'
    headers = {
        'Authorization': 'Token header-secret',
        'cOoKiE': 'session=cookie-secret',
        'Accept': 'application/json',
    }
    body = 'password=body-secret'


class RedactionTests(unittest.TestCase):

    def test_config_auth_token_is_redacted_without_losing_safe_values(self):
        config = {
            'server': {
                'auth_token': 'settings-secret',
                'url': 'https://app.obico.io',
            },
            'logging': {'level': 'DEBUG'},
            'webcams': [{'name': 'classic', 'target_fps': 15}],
        }

        result = redaction.redact_sensitive_data(config)

        self.assertEqual(redaction.REDACTED, result['server']['auth_token'])
        self.assertEqual('https://app.obico.io', result['server']['url'])
        self.assertEqual('DEBUG', result['logging']['level'])
        self.assertEqual('classic', result['webcams'][0]['name'])
        self.assertEqual(15, result['webcams'][0]['target_fps'])

    def test_authorization_and_cookie_headers_are_case_insensitively_redacted(self):
        result = redaction.redact_sensitive_data(DummyRequest.headers)

        self.assertEqual(redaction.REDACTED, result['Authorization'])
        self.assertEqual(redaction.REDACTED, result['cOoKiE'])
        self.assertEqual('application/json', result['Accept'])

    def test_url_credentials_and_sensitive_query_values_are_redacted(self):
        result = redaction.redact_url(
            'https://camera:password@example.com/live?TOKEN=secret&api_key=key-secret&fps=10&quality=high'
        )

        self.assertEqual(
            'https://<redacted>:<redacted>@example.com/live?TOKEN=<redacted>&api_key=<redacted>&fps=10&quality=high',
            result,
        )

    def test_redaction_does_not_modify_nested_input(self):
        original = {
            'auth_token': 'secret',
            'nested': [{'password': 'nested-secret', 'safe': 'readable'}],
        }
        snapshot = copy.deepcopy(original)

        redaction.redact_sensitive_data(original)

        self.assertEqual(snapshot, original)

    def test_http_request_keeps_diagnostics_but_never_logs_body(self):
        result = redaction.format_http_request(DummyRequest())

        self.assertIn('POST', result)
        self.assertIn('example.com/live', result)
        self.assertIn('fps=10', result)
        self.assertIn("'Accept': 'application/json'", result)
        self.assertNotIn('camera-password', result)
        self.assertNotIn('url-secret', result)
        self.assertNotIn('header-secret', result)
        self.assertNotIn('cookie-secret', result)
        self.assertNotIn('body-secret', result)

    def test_discovery_codes_are_redacted_but_ordinary_code_is_readable(self):
        data = {
            'one_time_passcode': '123456',
            'oneTimePasslink': 'https://app.obico.io/link?code=link-secret',
            'verification_code': 'verify-secret',
            'code': 503,
            'user_id': 'user-123',
        }

        result = redaction.redact_sensitive_data(data)

        self.assertEqual(redaction.REDACTED, result['one_time_passcode'])
        self.assertEqual(redaction.REDACTED, result['oneTimePasslink'])
        self.assertEqual(redaction.REDACTED, result['verification_code'])
        self.assertEqual(503, result['code'])
        self.assertEqual('user-123', result['user_id'])

        response_text = redaction.redact_text(
            '{"one_time_passcode":"123456","verification_code":"verify-secret","safe":"visible"}'
        )
        self.assertNotIn('123456', response_text)
        self.assertNotIn('verify-secret', response_text)
        self.assertIn('visible', response_text)

    def test_user_id_and_ordinary_diagnostic_identifiers_remain_visible(self):
        data = {
            'user_id': 'user-123',
            'userId': 'user-456',
            'session_id': 987654,
            'sessionId': 123456,
            'key': 'temperature',
            'public_key': 'public-material',
            'agent_signature': 'md5:file-fingerprint',
            'rotation': 90,
        }

        self.assertEqual(data, redaction.redact_sensitive_data(data))

    def test_http_tunnel_bodies_are_omitted_and_safe_metadata_remains_visible(self):
        message = {
            'http.tunnelv2': {
                'method': 'POST',
                'path': '/api/example',
                'params': {'token': 'query-secret', 'page': '2'},
                'headers': {'Authorization': 'Bearer header-secret', 'Accept': 'application/json'},
                'data': '{"token":"body-secret"}',
                'response': {'status': 200, 'content': 'response-secret'},
            },
        }

        result = redaction.redact_sensitive_data(message)['http.tunnelv2']

        self.assertEqual(redaction.REDACTED, result['data'])
        self.assertEqual(redaction.REDACTED, result['response']['content'])
        self.assertEqual(redaction.REDACTED, result['params']['token'])
        self.assertEqual(redaction.REDACTED, result['headers']['Authorization'])
        self.assertEqual('2', result['params']['page'])
        self.assertEqual('application/json', result['headers']['Accept'])
        self.assertEqual('/api/example', result['path'])

    def test_malformed_urls_and_unprintable_values_do_not_raise(self):
        class Unprintable(object):
            def __str__(self):
                raise ValueError('cannot serialize')

            def __repr__(self):
                raise ValueError('cannot serialize')

        malformed = 'http://[invalid-host/path?password=secret&mode=debug'
        encoded_name = 'http://[invalid-host/path?to%6ben=encoded-secret&mode=debug'

        result = redaction.redact_url(malformed)
        encoded_result = redaction.redact_url(encoded_name)

        self.assertNotIn('secret', result)
        self.assertNotIn('encoded-secret', encoded_result)
        self.assertIn('mode=debug', result)
        self.assertIn('mode=debug', encoded_result)
        self.assertIn('unprintable', redaction.redact_sensitive_data(Unprintable()))

    def test_logging_filter_redacts_dependency_messages_and_tracebacks(self):
        try:
            raise ValueError('password=traceback-secret')
        except ValueError:
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            'urllib3.connectionpool',
            logging.DEBUG,
            __file__,
            1,
            'GET %s',
            ('https://cam:userpass@example.com/live?token=query-secret&fps=10',),
            exc_info,
        )

        self.assertTrue(redaction.RedactingFilter().filter(record))
        rendered = record.getMessage()

        self.assertIn('GET', rendered)
        self.assertIn('example.com/live', rendered)
        self.assertIn('fps=10', rendered)
        self.assertNotIn('userpass', rendered)
        self.assertNotIn('query-secret', rendered)
        self.assertNotIn('traceback-secret', rendered)

    def test_logging_filter_redacts_structured_arguments(self):
        record = logging.LogRecord(
            'dependency',
            logging.INFO,
            __file__,
            1,
            'payload=%s',
            ({'auth_token': 'argument-secret', 'user_id': 'user-123'},),
            None,
        )

        redaction.RedactingFilter().filter(record)
        rendered = record.getMessage()

        self.assertNotIn('argument-secret', rendered)
        self.assertIn('user-123', rendered)


if __name__ == '__main__':
    unittest.main()
