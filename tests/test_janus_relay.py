import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from moonraker_obico import janus_config_builder as jcb  # noqa: E402

# What the server sends a printer as `relay` (obico-server lib/relay.py).
RELAY = {
    'ice_servers': [
        {'urls': ['stun:stun.l.google.com:19302']},
        {'urls': ['turn:relay.example.invalid:80?transport=tcp'], 'username': '1700000000:p42', 'credential': 'c2lnbmVk'},
    ],
    'expires_at': 1700000000,
}
STUN_ONLY = {'ice_servers': [{'urls': ['stun:stun.l.google.com:19302']}], 'expires_at': None}


class JanusRelayTestCase(unittest.TestCase):

    def build_jcfg(self, relay):
        with tempfile.TemporaryDirectory() as etc_dir, \
                mock.patch.object(jcb, 'RUNTIME_JANUS_ETC_DIR', etc_dir), \
                mock.patch.object(jcb, 'find_system_janus_paths', return_value=('/usr/bin/janus', '/usr/lib/janus')):
            jcb.build_janus_jcfg(relay)
            with open(os.path.join(etc_dir, 'janus.jcfg')) as f:
                return f.read()

    def test_janus_uses_the_relay_and_credential_from_the_server(self):
        jcfg = self.build_jcfg(RELAY)
        self.assertIn('turn_server = "relay.example.invalid"', jcfg)
        self.assertIn('turn_port = 80', jcfg)
        self.assertIn('turn_type = "tcp"', jcfg)
        self.assertIn('turn_user = "1700000000:p42"', jcfg)
        self.assertIn('turn_pwd = "c2lnbmVk"', jcfg)
        self.assertNotIn('turn.obico.io', jcfg)
        self.assertIn('nat: {\n', jcfg)
        self.assertEqual(jcfg.count('{'), jcfg.count('}'))

    def test_without_a_relay_from_the_server_janus_has_none(self):
        for relay in (STUN_ONLY, None):
            jcfg = self.build_jcfg(relay)
            self.assertNotIn('turn_', jcfg)
            self.assertIn('ignore_unreachable_ice_server = true', jcfg)

    def test_a_relay_url_janus_cannot_use_stops_the_build(self):
        relay = {'ice_servers': [{'urls': ['turns:relay.example.invalid:443?transport=tcp'], 'username': 'u', 'credential': 'c'}]}
        with self.assertRaises(ValueError):
            jcb.janus_relay_lines(relay)

    def test_renewal_is_due_a_day_before_the_credential_expires(self):
        self.assertFalse(jcb.relay_renewal_due(RELAY, 1700000000 - 24 * 3600 - 1))
        self.assertTrue(jcb.relay_renewal_due(RELAY, 1700000000 - 24 * 3600))
        self.assertFalse(jcb.relay_renewal_due(STUN_ONLY, 1700000000))
        self.assertFalse(jcb.relay_renewal_due(None, 1700000000))


if __name__ == '__main__':
    unittest.main()
