import types
import unittest
from unittest import mock

from moonraker_obico.tunnel import LocalTunnel

def _tunnel(responses):
    cfg = types.SimpleNamespace(dest_is_ssl=False, dest_host='printer', dest_port='80', url_blacklist=[])
    return LocalTunnel(cfg, lambda m, as_binary: responses.append(m['http.tunnelv2']['response']), None, None)

def _fake_resp(status, content, headers=None):
    resp = mock.Mock(status_code=status, content=content, headers=headers or {})
    resp.raw._original_response.msg.get_all.return_value = None
    return resp

def _get(status, content):
    responses = []
    with mock.patch('moonraker_obico.tunnel.requests.get', return_value=_fake_resp(status, content)):
        _tunnel(responses).send_http_to_local_v2('ref', 'get', '/config.json', headers={'If-None-Match': '"x"'})
    return responses[0]

class ConfigJsonTests(unittest.TestCase):
    def test_config_json_304_passes_through(self):
        resp = _get(304, b'')
        assert resp['status'] == 304
        assert resp['content'] == b''

    def test_config_json_error_with_non_json_body_passes_through(self):
        resp = _get(404, b'<html>Not Found</html>')
        assert resp['status'] == 404
        assert resp['content'] == b'<html>Not Found</html>'

    def test_config_json_200_still_rewrites_instances_db(self):
        resp = _get(200, b'{"instancesDB": "browser", "instances": [{"hostname": "x"}]}')
        assert resp['status'] == 200
        assert b'"instancesDB": "moonraker"' in resp['content']
        assert b'"instances": []' in resp['content']
