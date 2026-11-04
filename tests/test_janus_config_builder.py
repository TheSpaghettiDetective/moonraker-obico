import importlib.util
import os
import sys
import types
import unittest


PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# janus_config_builder lives inside the agent package and pulls in third-party
# utils (sentry_sdk, sarge, distro, ...) at import time. Stub whatever is not
# installed so the test runs in a bare environment.
def _stub(name, **attrs):
    if importlib.util.find_spec(name) is not None:
        return importlib.import_module(name)
    mod = types.ModuleType(name)
    mod.__path__ = []  # make it a package so dotted imports resolve
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    parent = name.rpartition('.')[0]
    if parent:
        setattr(sys.modules[parent], name.rpartition('.')[2], mod)
    return mod


_stub('sentry_sdk', capture_exception=lambda *a, **kw: None, init=lambda *a, **kw: None)
_stub('sentry_sdk.integrations')
_stub('sentry_sdk.integrations.threading', ThreadingIntegration=object)
_stub('sentry_sdk.integrations.logging', LoggingIntegration=object)
_stub('sentry_sdk.integrations.flask', FlaskIntegration=object)
_stub('sarge', run=lambda *a, **kw: None, Capture=type('Capture', (), {}))
_stub('distro')
_backoff = _stub('backoff', expo=object)
_backoff.__getattr__ = lambda name: (lambda *a, **kw: (lambda fn: fn))  # on_exception, on_predicate, ...
_stub('requests')
_stub('octoprint')
_stub('octoprint.util')
_stub('octoprint.util.version', get_octoprint_version_string=lambda: '0.0.0')

pkg = types.ModuleType('moonraker_obico')
pkg.__path__ = [os.path.join(PKG_DIR, 'moonraker_obico')]
sys.modules.setdefault('moonraker_obico', pkg)

MODULE_PATH = os.path.join(PKG_DIR, 'moonraker_obico', 'janus_config_builder.py')
SPEC = importlib.util.spec_from_file_location('moonraker_obico.janus_config_builder', MODULE_PATH)
jcb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jcb)


class TurnCredentialExpiryWarningTestCase(unittest.TestCase):
    def test_returns_none_without_turn(self):
        self.assertIsNone(jcb.turn_credential_expiry_warning(None, 'Obico for Klipper'))
        self.assertIsNone(jcb.turn_credential_expiry_warning({}, 'Obico for Klipper'))

    def test_non_dict_turn_does_not_raise(self):
        # A malformed turn value must not raise out of the hourly expiry check,
        # otherwise the loop spam-sentry-captures every hour.
        for bad in ('string', 42, True, ['a'], ('a',)):
            self.assertIsNone(jcb.turn_credential_expiry_warning(bad, 'Obico for Klipper'))

    def test_static_credential_returns_none(self):
        self.assertIsNone(
            jcb.turn_credential_expiry_warning({'expires_at': None}, 'Obico for Klipper'))

    def test_expired_or_soon_expiring_returns_message(self):
        title, text = jcb.turn_credential_expiry_warning({'expires_at': 1}, 'Obico for Klipper')
        self.assertIn('expired', title)
        self.assertIn('restart', text.lower())


class TurnSettingsTestCase(unittest.TestCase):
    def test_server_turn_replaces_default(self):
        server, port, transport, username, credential = jcb.turn_settings(
            'authtok',
            {
                'server': 'turn.example.com',
                'port': 3478,
                'transports': ['udp', 'tcp'],
                'username': '1700003600:printer-1',
                'credential': 'secret123',
            },
        )
        self.assertEqual(server, 'turn.example.com')
        self.assertEqual(port, 3478)
        self.assertEqual(transport, 'udp')
        self.assertEqual(username, '1700003600:printer-1')
        self.assertEqual(credential, 'secret123')

    def test_invalid_server_turn_falls_back_to_default(self):
        self.assertEqual(
            jcb.turn_settings('authtok', {'server': 'bad"server', 'port': 3478}),
            (jcb.DEFAULT_TURN['server'], jcb.DEFAULT_TURN['port'], jcb.DEFAULT_TURN['transport'], 'authtok', 'authtok'),
        )

    def test_tls_transport_passthrough(self):
        server, port, transport, _, _ = jcb.turn_settings(
            'authtok',
            {
                'server': 'turn.example.com',
                'port': 443,
                'transports': ['tls'],
                'username': '1700003600:printer-1',
                'credential': 'secret123',
            },
        )
        self.assertEqual(server, 'turn.example.com')
        self.assertEqual(port, 443)
        self.assertEqual(transport, 'tls')


if __name__ == '__main__':
    unittest.main()
